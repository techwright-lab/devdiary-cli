# frozen_string_literal: true

# Actual captured rows only. Invoked by qualify_claude.py with an owned test DB,
# once per stage, both on QUALIFICATION_PORT:
#   pair     `devdiary setup` pairs this machine against Rails on a terminal.
#   deliver  the stock session's rows go out with `sync CONNECTION_DIR`; the
#            first answer is lost after commit and must replay the same bytes.
require "json"
require "uri"
require "pathname"
require "open3"
require_relative "qualification_cleanup"
require_relative "rails_setup"
stage = ARGV.fetch(0)
raise "stage must be pair or deliver" unless %w[pair deliver].include?(stage)
raise "test required" unless ENV["RAILS_ENV"] == "test"
url = URI(ENV.fetch("DATABASE_URL"))
raise "isolated database required" unless url.host == "127.0.0.1" && url.path.match?(%r{\A/devdiary_qualification_[0-9a-f]{32}\z})
root = Pathname.new(ENV.fetch("QUALIFICATION_ROOT"))
RailsSetup.isolated!(root)
port = Integer(ENV.fetch("QUALIFICATION_PORT"))
require File.join(Dir.pwd, "config/boot")
require_relative "qualification_database"
database_guard = QualificationDatabase.new(ENV.fetch("DATABASE_URL"))
database_guard.install!
require File.join(Dir.pwd, "config/environment")
# No rake schema task: it may enumerate extra configs or establish a new target.
# Validate the real resolver AND connected application in the same Ruby process.
database_guard.verify_application!
if stage == "pair"
  database_guard.load_schema!(Dir.pwd, defined?(Rails) ? Rails.application.config.active_record.schema_format : :ruby)
  ActiveRecord::Migration.suppress_messages do
    ActiveRecord::Base.connection_pool.migration_context.migrate
  end
  database_guard.verify_application!
end
require "factory_bot_rails"
require "webmock"
require "puma"
require "net/http"
require "warden/test/helpers"
include Warden::Test::Helpers
Warden.test_mode!
WebMock.disable_net_connect!(allow_localhost: true)
Rails.logger = ActiveSupport::Logger.new(File::NULL)
ActionController::Base.allow_forgery_protection = true
FactoryBot.find_definitions if FactoryBot.factories.count.zero?
database_guard.verify_application!
Signal.trap("TERM") { raise Interrupt }
exe = root.join("collector").to_s
connection = Pathname.new(ENV.fetch("HOME")).join(".local/share/devdiary/cli/claude/connection")
secret = /dc_live_|dp_pair_/
origin = "http://127.0.0.1:#{port}"

if stage == "pair"
  raise "database not empty" unless CollectorObservation.count.zero? && Workspace.count.zero?
  user = FactoryBot.create(:onboarded_user)
  workspace = user.current_team.workspace || FactoryBot.create(:workspace, owner: user).tap { |w| user.current_team.update!(workspace: w) }
  repository = FactoryBot.create(:repository, full_name: "fixture/claude-qualification")
  WorkspaceRepository.create!(workspace: workspace, repository: repository, active: true)
  server, = RailsSetup.serve(Rails.application, port)
  begin
    http = Net::HTTP.new("127.0.0.1", port, nil)
    http.open_timeout = http.read_timeout = 10
    connected = false
    status, output = RailsSetup.terminal([exe, "setup", "--origin", origin, "--trust-origin", "--no-browser"], answers: %w[y]) do |code|
      login_as(user, scope: :user)
      _, connected = RailsSetup.approve(http, code, workspace_id: workspace.id)
    end
    raise "secret output" if output.match?(secret)
    raise "setup exited #{status}: #{output[/error_code: \w+/]}" unless status.zero? && connected
    raise "installation credential" unless CollectorCredential.sole.installation_scope?
  ensure
    server.stop(true)
    Warden.test_reset!
  end
  exit 0
end

actual = JSON.parse(root.join("observations.json").read)
raise "unexpected runtime rows" unless actual.size == 6 && actual.map { |r| r["event"] }.sort == %w[PostToolUse PreToolUse SessionEnd SessionStart Stop UserPromptSubmit].sort
token = JSON.parse(connection.join("connection.json").read).fetch("token")
collector = CollectorCredential.authenticate(token) || raise("connection not paired here")
state = {drop: true, requests: []}
app = lambda do |env|
  if env.fetch("PATH_INFO") == "/ingest/v1/observations"
    state[:requests] << env["rack.input"].read
    env["rack.input"].rewind
  end
  status, headers, body = Rails.application.call(env)
  if state[:drop] && env.fetch("PATH_INFO") == "/ingest/v1/observations" && status == 201
    state[:drop] = false
    body.close if body.respond_to?(:close)
    [502, {"content-type" => "text/plain"}, ["fixture response loss after application commit"]]
  else
    [status, headers, body]
  end
end
server = nil
report = {"passed" => false, "collector_revoked" => false}
begin
  server, = RailsSetup.serve(app, port)
  collector_command = lambda do |*args|
    # The CLI caps every HTTP attempt at five seconds. The parent has a group deadline.
    out, err, status = Open3.capture3(exe, *args)
    raise "#{args.first} exited #{status.exitstatus}: #{err[/error_code: \w+/]}" unless status.success?
    raise "credential leak" if (out + err).match?(secret)
    out
  end
  before = [WorkSession.count, SessionEvent.count, Attribution.count]
  # The scheduler's sync exits 0 and leaves the failure for doctor.
  collector_command.call("sync", connection.to_s)
  lost = JSON.parse(collector_command.call("doctor", "--json"))
  raise "response loss not reported" unless lost["last_sync_error"] && lost["pending"].positive?
  first = CollectorObservation.order(:id).first
  raise "response-loss not committed" unless first&.metadata&.fetch("observation_id") == JSON.parse(state[:requests].first)["observation_id"]
  first_id = first.id
  collector_command.call("sync", connection.to_s)
  remote = CollectorObservation.order(:id).map(&:metadata)
  raise "payload mismatch" unless remote.sort_by { |r| r.fetch("observation_id") } == actual.sort_by { |r| r.fetch("observation_id") }
  raise "replay changed" unless state[:requests].count(state[:requests].first) == 2
  raise "duplicate record" unless CollectorObservation.count == actual.size && CollectorObservation.exists?(first_id)
  raise "actor invented" unless remote.all? { |r| r["attribution_basis"] == "unknown" && !r.key?("actor_ref") }
  raise "private metadata" if remote.to_json.match?(/PRIVATE_qualify|fixture\.txt|\/home\//) || remote.to_json.include?(root.to_s)
  raise "attribution side effect" unless before == [WorkSession.count, SessionEvent.count, Attribution.count]
  expected_receipts = CollectorObservation.order(:id).map do |record|
    metadata = record.metadata
    {"collector_ref" => collector.collector_ref, "observation_id" => metadata.fetch("observation_id"),
     "installation_id" => metadata.fetch("installation_id"), "record_id" => record.id}
  end
  root.join("expected-receipts.json").write(JSON.generate(expected_receipts))
  report.merge!("passed" => true, "actual_stock_records" => actual.size, "http_requests" => state[:requests].size,
    "payload_equality" => true, "exact_replay" => true, "no_attribution_side_effects" => true)
ensure
  # Each independent stage runs even when every preceding stage failed.
  QualificationCleanup.run(report, {
    "revoke" => -> {
      collector.update!(revoked_at: Time.current)
      raise "revocation failed" unless collector.reload.revoked_at && CollectorCredential.authenticate(token).nil?
      report["collector_revoked"] = true
    },
    "server_stop" => -> { server&.stop(true) }
  }, ->(text) { root.join("rails-result.json").write(text) })
end
