# frozen_string_literal: true
# Real Rails HTTP + PostgreSQL; only login is a Warden test fixture. Never a host/model run.
require "json"
require "uri"
require "pathname"
require "securerandom"
require "open3"
require "timeout"
require "net/http"
require_relative "qualification_cleanup"
raise "test required" unless ENV.fetch("RAILS_ENV") == "test"
root = Pathname.new(ENV.fetch("PAIRING_ROOT"))
require File.join(Dir.pwd, "config/boot")
require_relative "qualification_database"
guard = QualificationDatabase.new(ENV.fetch("DATABASE_URL"))
guard.install!
require File.join(Dir.pwd, "config/environment")
guard.verify_application!
ActiveRecord::Migration.suppress_messages { load File.join(Dir.pwd, "db/schema.rb") }
guard.verify_application!
require "factory_bot_rails"
require "webmock"
require "puma"
require "warden/test/helpers"
include Warden::Test::Helpers
Warden.test_mode!
WebMock.disable_net_connect!(allow_localhost: true)
# Bring the stale checked-in snapshot to this reviewed server head, in the
# same guarded process. No schema dump or checkout/config mutation.
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
link = WorkspaceRepository.create!(workspace: workspace, repository: repo, active: true)
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
server = Puma::Server.new(app, nil, {min_threads: 0, max_threads: 2})
server.add_tcp_listener("127.0.0.1", 0)
port = server.binder.ios.first.addr[1]
server.run
origin = "http://127.0.0.1:#{port}"
exe = root.join("collector").to_s
repository = root.join("repo").to_s
connection = root.join("connection").to_s
http = Net::HTTP.new("127.0.0.1", port, nil)
http.open_timeout = http.read_timeout = 10
routes = Rails.application.routes.url_helpers
browser = lambda do |code, selection|
  login_as(user, scope: :user)
  get = http.get(routes.phoenix_settings_collector_path)
  raise "browser GET #{get.code}" unless get.code == "200"
  doc = Nokogiri::HTML(get.body)
  csrf = doc.at_css('meta[name="csrf-token"]')&.[]("content") || doc.at_css('input[name="authenticity_token"]')&.[]("value")
  raise "real CSRF missing" unless csrf
  cookie = get.get_fields("set-cookie").map { |c| c.split(";", 2).first }.join("; ")
  values = {user_code: code, workspace_id: workspace.id, workspace_repository_id: selection, bind_interactive: "1"}
  post = Net::HTTP::Post.new(routes.approve_phoenix_settings_collector_path)
  post["Cookie"] = cookie
  post.set_form_data(values)
  denied = http.request(post)
  raise "CSRF bypassed #{denied.code}" unless denied.code == "422"
  raise "CSRF mutated pairing" unless CollectorPairing.order(:id).last.status == "pending"
  post.set_form_data(values.merge(authenticity_token: csrf))
  approved = http.request(post)
  raise "approval #{approved.code}" unless approved.code == "303"
  raise "approval not durable" unless CollectorPairing.order(:id).last.status == "approved"
end
command = lambda do |args, data = "", expected = 0|
  out, err, status = Open3.capture3(exe, *args, stdin_data: data)
  raise "command failed #{args.first}: #{status.exitstatus}" unless status.exitstatus == expected
  raise "secret output" if (out + err).match?(/dc_live_|dp_pair_/)
  out
end
pair = lambda do |extra, expected, target: [connection, repository], selection: link.id|
  Open3.popen3(exe, "setup", *target, "--origin", origin, "--trust-origin", "--no-browser", *extra) do |stdin, stdout, stderr, waiter|
    stdin.close
    output = +""
    Timeout.timeout(25) do
      code = nil
      until code
        line = stdout.gets || raise("no approval code: #{stderr.read.strip}")
        output << line
        code = line[/One-time code: ([a-f0-9]{32})/, 1]
      end
      browser.call(code, selection)
      output << stdout.read
      output << stderr.read
      raise "setup result" unless waiter.value.exitstatus == expected
    end
    raise "secret output" if output.match?(/dc_live_|dp_pair_/)
  end
end
report = {"passed" => false}
begin
  state[:drop_pair] = true
  pair.call([], 2)
  raise "lost exchange did not commit" unless CollectorCredential.count == 1 && CollectorPairing.last.status == "consumed"
  raise "client pretended credential recovery" if File.exist?(File.join(connection, "connection.json"))
  replay = Net::HTTP::Post.new("/ingest/v1/collector_pairings/exchange")
  replay["Authorization"] = state[:bearer]
  replay["Content-Type"] = "application/json"
  replay.body = "{}"
  gone = http.request(replay)
  raise "credential replayed" unless gone.code == "410" && JSON.parse(gone.body) == {"error" => "consumed"}
  # Emulate customer's explicit revocation repair in owned fixture DB.
  CollectorCredential.first.update!(revoked_at: Time.current)
  pair.call(["--new-pair"], 0)
  config = JSON.parse(File.read(File.join(connection, "connection.json")))
  scoped = CollectorCredential.authenticate(config.fetch("token"))
  raise "wrong server scope" unless scoped.repository_id == repo.id && scoped.runtime == "claude-code" && scoped.installation_id == config.dig("scope", "installation_id")
  raise "permissions" unless (File.stat(File.join(connection, "connection.json")).mode & 0o777) == 0o600
  settings = root.join("settings.json")
  settings.write(JSON.generate({"env" => {"CUSTOMER" => "keep"}, "hooks" => {"Stop" => []}}))
  settings.chmod(0o600)
  plan = root.join("plan.json")
  original = settings.read
  command.call(["setup-plan", connection, settings.to_s, plan.to_s])
  raise "plan mutated settings" unless settings.read == original
  command.call(["claude-apply", plan.to_s, "--consent"])
  command.call(["claude-remove", plan.to_s, "--consent"])
  raise "settings not preserved" unless JSON.parse(settings.read) == JSON.parse(original)
  observation = {schema_version: 1, observation_id: SecureRandom.uuid, installation_id: scoped.installation_id,
                 repository: repository, runtime: "claude-code", session_id: "fixture-pairing", event: "SessionStart",
                 observed_at: Time.now.to_f, attribution_basis: "unknown"}
  command.call(["collect", File.join(connection, "outbox")], JSON.generate(observation))
  state[:drop_ingest] = true
  command.call(["connection-sync", connection, "10"], "", 1)
  raise "ingest loss not committed" unless CollectorObservation.count == 1
  record_id = CollectorObservation.first.id
  delivered = JSON.parse(command.call(["connection-sync", connection, "10"]))
  raise "sync not delivered" unless delivered == {"pending" => 0, "delivered" => 1}
  raise "replay not identical" unless state[:requests].size == 2 && state[:requests].uniq.size == 1 && CollectorObservation.count == 1 && CollectorObservation.first.id == record_id
  payload = JSON.parse(state[:requests].first)
  other = FactoryBot.create(:repository, full_name: "fixture/other")
  WorkspaceRepository.create!(workspace: workspace, repository: other, active: true)
  [{"repository_ref" => "https://github.com/fixture/other"}, {"installation_id" => SecureRandom.uuid}, {"runtime" => "codex"}, {"attribution_basis" => "explicit_local_binding", "actor_ref" => "invented"}].each do |changes|
    request = Net::HTTP::Post.new("/ingest/v1/observations")
    request["Authorization"] = "Bearer #{config.fetch('token')}"
    request["Content-Type"] = "application/json"
    request.body = JSON.generate(payload.merge(changes))
    rejected = http.request(request)
    raise "scope rejected with unexpected status #{changes.keys.join(',')}: #{rejected.code}" unless rejected.code == "403"
  end
  raise "repository scope did not send v1" unless payload["schema_version"] == 1
  installation = root.join("installation-connection").to_s
  pair.call([], 0, target: [installation], selection: "all")
  saved = JSON.parse(File.read(File.join(installation, "connection.json")))
  raise "installation scope" unless saved.dig("scope", "scope_kind") == "installation" &&
    saved.dig("scope", "observation_schema_versions") == [1, 2] && saved["repository_refs"] == %w[https://github.com/fixture/other https://github.com/fixture/pairing]
  credential = CollectorCredential.authenticate(saved.fetch("token"))
  raise "installation credential" unless credential.installation_scope? && credential.owner_user_id == user.id && credential.bind_interactive
  hook_settings = root.join("installation-settings.json")
  hook_settings.write("{}")
  hook_settings.chmod(0o600)
  hook_plan = root.join("installation-plan.json").to_s
  command.call(["setup-plan", installation, hook_settings.to_s, hook_plan])
  command.call(["claude-apply", hook_plan, "--consent"])
  agent = SecureRandom.uuid
  [[{"PAPERCLIP_AGENT_ID" => agent, "PAPERCLIP_API_KEY" => "SENTINELSECRET"}, "PostToolUse"], [{}, "SessionStart"]].each do |env, event|
    hook_input = JSON.generate({hook_event_name: event, session_id: "fixture-installation", cwd: repository, tool_use_id: "toolu_fixture", tool_name: "Bash"})
    out, err, status = Open3.capture3(env, exe, "claude-hook", hook_plan, stdin_data: hook_input)
    raise "hook not silent" unless status.success? && out.empty? && err.empty?
  end
  sent = state[:requests].size
  raise "installation sync" unless JSON.parse(command.call(["connection-sync", installation, "10"])) == {"pending" => 0, "delivered" => 2}
  wire = state[:requests][sent..].map { |body| JSON.parse(body) }
  raise "installation scope did not send v2" unless wire.all? { |row| row["schema_version"] == 2 && !row.key?("actor_ref") } &&
    wire.map { |row| row["orchestrator"] }.sort == %w[none paperclip]
  raise "secret on the wire" if state[:requests].join.include?("SENTINELSECRET")
  raise "scope endpoint not read" unless state[:scope_reads].positive?
  rows = CollectorObservation.where(collector_credential: credential).to_a
  raise "v2 rows not stored" unless rows.size == 2 && rows.all? { |row| row.repository_id == repo.id && row.metadata["schema_version"] == 2 }
  raise "agent ref" unless rows.map { |row| row.metadata["orchestrator_agent_ref"] }.compact == [agent]
  command.call(["claude-remove", hook_plan, "--consent"])
  raise "authorship changed" unless before == [WorkSession.count, SessionEvent.count, Attribution.count]
  raise "identity invented" unless CollectorObservation.first.metadata["attribution_basis"] == "unknown"
  guard.verify_application!
  report.merge!({"passed" => true, "browser_real_csrf" => true, "lost_pairing_response_new_pair" => true, "consumed_replay_rejected" => true, "exact_ingest_replay" => true, "scoped_rejections" => 4, "unknown_authorship" => true, "plan_apply_remove_preserved_settings" => true, "repository_scope_v1" => true, "installation_scope_v2" => true, "record_id" => record_id, "observation_id" => observation[:observation_id]})
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
