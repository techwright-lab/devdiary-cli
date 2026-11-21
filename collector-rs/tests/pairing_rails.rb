# frozen_string_literal: true

# `devdiary setup` on a terminal against real Rails HTTP + PostgreSQL. Only login
# is a Warden test fixture. Never a host or model run: hook input is synthetic.
require "json"
require "uri"
require "pathname"
require "securerandom"
require "open3"
require "fileutils"
require_relative "qualification_cleanup"
require_relative "rails_setup"
raise "test required" unless ENV.fetch("RAILS_ENV") == "test"
root = Pathname.new(ENV.fetch("PAIRING_ROOT"))
RailsSetup.isolated!(root)
require File.join(Dir.pwd, "config/boot")
require_relative "qualification_database"
guard = QualificationDatabase.new(ENV.fetch("DATABASE_URL"))
guard.install!
require File.join(Dir.pwd, "config/environment")
guard.verify_application!
guard.load_schema!(Dir.pwd, Rails.application.config.active_record.schema_format)
require "factory_bot_rails"
require "webmock"
require "puma"
require "net/http"
require "warden/test/helpers"
include Warden::Test::Helpers
Warden.test_mode!
WebMock.disable_net_connect!(allow_localhost: true)
# Bring the loaded schema to this reviewed server head, in the same guarded
# process. No schema dump or checkout/config mutation.
ActiveRecord::Migration.suppress_messages do
  ActiveRecord::Base.connection_pool.migration_context.migrate
end
guard.verify_application!
Rails.logger = ActiveSupport::Logger.new(File::NULL)
ActionController::Base.allow_forgery_protection = true
FactoryBot.find_definitions if FactoryBot.factories.count.zero?
raise "database not empty" unless Workspace.count.zero? && CollectorCredential.count.zero?
user = FactoryBot.create(:onboarded_user)
workspace = user.current_team.workspace || FactoryBot.create(:workspace, owner: user).tap { |w| user.current_team.update!(workspace: w) }
repo = FactoryBot.create(:repository, full_name: "fixture/pairing")
other = FactoryBot.create(:repository, full_name: "fixture/other")
[repo, other].each { |r| WorkspaceRepository.create!(workspace: workspace, repository: r, active: true) }
before = [WorkSession.count, SessionEvent.count, Attribution.count]
state = {drop_pair: false, drop_ingest: false, bearer: nil, requests: [], scope_reads: 0}
app = lambda do |env|
  path = env.fetch("PATH_INFO")
  state[:bearer] = env["HTTP_AUTHORIZATION"] if path.end_with?("/exchange")
  state[:scope_reads] += 1 if path == "/ingest/v1/collector/scope"
  if path == "/ingest/v1/observations"
    body = env["rack.input"].read
    env["rack.input"].rewind
    state[:requests] << body
  end
  status, headers, body = Rails.application.call(env)
  if (state[:drop_pair] && path.end_with?("/exchange") && status == 200 && CollectorPairing.order(:id).last.status == "consumed") ||
      (state[:drop_ingest] && path == "/ingest/v1/observations" && status == 201)
    state[:drop_pair] = state[:drop_ingest] = false
    body.close if body.respond_to?(:close)
    [502, {"content-type" => "text/plain"}, ["fixture response loss after application commit"]]
  else
    [status, headers, body]
  end
end
server, port = RailsSetup.serve(app)
origin = "http://127.0.0.1:#{port}"
exe = root.join("collector").to_s
repository = root.join("repo").to_s
home = Pathname.new(ENV.fetch("HOME"))
data = home.join(".local/share/devdiary/cli/claude")
connection = data.join("connection")
settings = home.join(".claude/settings.json")
original = JSON.generate({"env" => {"CUSTOMER" => "keep"}, "hooks" => {"Stop" => []}})
settings.write(original)
settings.chmod(0o600)
http = Net::HTTP.new("127.0.0.1", port, nil)
http.open_timeout = http.read_timeout = 10
secret = /dc_live_|dp_pair_/
command = lambda do |args, expected = 0|
  out, err, status = Open3.capture3(exe, *args)
  raise "#{args.first} exited #{status.exitstatus}: #{err[/error_code: \w+/]}" unless status.exitstatus == expected
  raise "secret output" if (out + err).match?(secret)
  out
end
doctor = -> { JSON.parse(command.call(["doctor"])) }
setup = lambda do |expected, answers|
  verifier = connected = nil
  status, output = RailsSetup.terminal([exe, "setup", "--origin", origin, "--trust-origin", "--no-browser"], answers:) do |code|
    login_as(user, scope: :user)
    verifier, connected = RailsSetup.approve(http, code, workspace_id: workspace.id)
  end
  raise "secret output" if output.match?(secret)
  raise "setup exited #{status}: #{output[/error_code: \w+/]}" unless status == expected
  [output, verifier, connected]
end
hook = lambda do |event, env = {}|
  record = JSON.parse(data.join("install.json").read)
  input = JSON.generate({hook_event_name: event, session_id: "fixture-installation", cwd: repository, tool_use_id: "toolu_fixture", tool_name: "Bash"})
  out, err, status = Open3.capture3(env, record.fetch("executable"), "hook", "claude", record.fetch("plan"), stdin_data: input)
  raise "hook not silent" unless status.success? && out.empty? && err.empty?
end
repair = "move the connection folder aside and run setup again"
report = {"passed" => false}
begin
  # The exchange commits on the server but its answer never reaches the CLI.
  state[:drop_pair] = true
  output, verifier, connected = setup.call(2, %w[y])
  raise "lost exchange: #{output[/error_code: \w+/]}, connected #{connected}" unless output.include?("error_code: setup_incomplete") && output.include?(repair) && !connected
  raise "lost exchange did not commit" unless CollectorCredential.count == 1 && CollectorPairing.last.status == "consumed"
  raise "client pretended credential recovery" if connection.join("connection.json").exist?
  raise "hooks written without a connection" unless settings.read == original
  replay = Net::HTTP::Post.new("/ingest/v1/collector_pairings/exchange")
  replay["Authorization"] = state[:bearer]
  replay["Content-Type"] = "application/json"
  replay.body = JSON.generate({schema_version: 2, verifier: verifier})
  gone = http.request(replay)
  raise "credential replayed" unless gone.code == "410" && JSON.parse(gone.body) == {"error" => "consumed"}
  # Setup will not pair over that attempt. The person revokes the unused
  # connection in the browser and moves the folder aside, as setup says.
  output, = setup.call(2, %w[y])
  raise "repair not asked: #{output[/error_code: \w+/]}" unless output.include?("error_code: setup_incomplete") && output.include?(repair) && CollectorPairing.count == 1
  CollectorCredential.first.update!(revoked_at: Time.current)
  FileUtils.mv(connection, data.join("connection.moved-aside"))
  # A fresh pairing.
  output, _, connected = setup.call(0, %w[y])
  raise "callback page" unless connected
  raise "workspace not named" unless output.include?("Connected to the DevDiary workspace")
  raise "recording not on" unless output.include?("Recording is on for Claude Code.")
  saved = JSON.parse(connection.join("connection.json").read)
  raise "installation scope" unless saved.dig("scope", "scope_kind") == "installation" &&
    saved.dig("scope", "observation_schema_versions") == [1, 2] &&
    saved["repository_refs"] == %w[https://github.com/fixture/other https://github.com/fixture/pairing]
  raise "permissions" unless (connection.join("connection.json").stat.mode & 0o777) == 0o600
  credential = CollectorCredential.authenticate(saved.fetch("token"))
  raise "installation credential" unless credential.installation_scope? && credential.owner_user_id == user.id &&
    credential.bind_interactive && credential.runtime == "claude-code" && credential.installation_id == saved.dig("scope", "installation_id")
  written = JSON.parse(settings.read)
  raise "settings not kept" unless written["env"] == {"CUSTOMER" => "keep"} && written.dig("hooks", "SessionStart")
  # Hooks capture, reading only the environment names in the approved list;
  # then a lost ingest answer must replay the same bytes.
  File.write(connection.join("outbox/orchestrators-approved.json"),
    JSON.generate([{key: "paperclip", env: {agent_ref: "PAPERCLIP_AGENT_ID"}}]), perm: 0o600)
  agent = SecureRandom.uuid
  hook.call("PostToolUse", {"PAPERCLIP_AGENT_ID" => agent, "PAPERCLIP_API_KEY" => "SENTINELSECRET"})
  hook.call("SessionStart")
  # The scheduler's sync exits 0 and leaves the failure for doctor.
  state[:drop_ingest] = true
  command.call(["sync", connection.to_s])
  lost = doctor.call
  raise "ingest loss not reported" unless lost["last_sync_error"] && lost["pending"].positive?
  first = CollectorObservation.order(:id).first
  raise "ingest loss not committed" unless first&.observation_id == JSON.parse(state[:requests].first)["observation_id"]
  command.call(["sync", connection.to_s])
  raise "sync not delivered" unless doctor.call.values_at("pending", "delivered") == [0, 2]
  raise "replay not identical" unless state[:requests].count(state[:requests].first) == 2 &&
    CollectorObservation.count == 2 && CollectorObservation.exists?(first.id)
  wire = state[:requests].uniq.map { |body| JSON.parse(body) }
  raise "installation scope did not send v2" unless wire.all? { |row| row["schema_version"] == 2 && row["attribution_basis"] == "unknown" && !row.key?("actor_ref") } &&
    wire.map { |row| row["orchestrator"] }.compact == %w[paperclip]
  raise "secret on the wire" if state[:requests].join.include?("SENTINELSECRET")
  raise "scope endpoint not read" unless state[:scope_reads].positive?
  rows = CollectorObservation.where(collector_credential: credential).order(:id).to_a
  raise "v2 rows not stored" unless rows.size == 2 && rows.all? { |row| row.repository_id == repo.id && row.metadata["schema_version"] == 2 }
  raise "agent ref" unless rows.map { |row| row.metadata["orchestrator_agent_ref"] }.compact == [agent]
  # The credential binds the installation and runtime; the server also refuses
  # an invented actor and an untracked repository. Nothing is stored.
  stored = CollectorObservation.count
  [[{"installation_id" => SecureRandom.uuid}, "403"], [{"runtime" => "codex"}, "403"],
    [{"attribution_basis" => "explicit_local_binding", "actor_ref" => "invented"}, "422"],
    [{"repository_ref" => "https://github.com/fixture/untracked"}, "422"]].each do |change, code|
    request = Net::HTTP::Post.new("/ingest/v1/observations")
    request["Authorization"] = "Bearer #{saved.fetch("token")}"
    request["Content-Type"] = "application/json"
    request.body = JSON.generate(wire.first.merge(change, "observation_id" => SecureRandom.uuid))
    answer = http.request(request)
    raise "#{change.keys.join(",")} answered #{answer.code}, not #{code}" unless answer.code == code
  end
  raise "refused rows stored" unless CollectorObservation.count == stored
  # Revocation: a row captured before it stays pending here and is never stored.
  hook.call("Stop")
  credential.update!(revoked_at: Time.current)
  command.call(["sync", connection.to_s])
  status = doctor.call
  raise "revocation not retained" unless status["revoked"] && status["pending"] == 1 && CollectorObservation.count == stored
  command.call(["uninstall"])
  raise "settings not restored" unless JSON.parse(settings.read) == JSON.parse(original)
  raise "authorship changed" unless before == [WorkSession.count, SessionEvent.count, Attribution.count]
  raise "identity invented" unless CollectorObservation.all.all? { |row| row.metadata["attribution_basis"] == "unknown" }
  guard.verify_application!
  report.merge!({"passed" => true, "browser_real_csrf" => true, "lost_exchange_repaired_by_hand" => true,
                 "consumed_replay_rejected" => true, "exact_ingest_replay" => true, "scoped_rejections" => 4,
                 "approved_orchestrator_env_only" => true, "revocation_keeps_pending" => true,
                 "uninstall_restored_settings" => true, "unknown_authorship" => true, "installation_scope_v2" => true,
                 "receipts" => rows.map { |row|
                   {"record_id" => row.id, "collector_ref" => credential.collector_ref,
                    "installation_id" => row.installation_id, "observation_id" => row.observation_id}
                 }})
ensure
  QualificationCleanup.run(report, {
    "revoke" => -> {
      CollectorCredential.update_all(revoked_at: Time.current)
      raise "revocation cleanup" if CollectorCredential.where(revoked_at: nil).exists?
      report["collector_revoked"] = true
    },
    "server_stop" => -> { server.stop(true) },
    "warden_reset" => -> { Warden.test_reset! }
  }, ->(text) { root.join("pairing-result.json").write(text) })
end
