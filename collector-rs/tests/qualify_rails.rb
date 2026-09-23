# frozen_string_literal: true

# Actual captured rows only. Invoked by qualify_claude.py with an owned test DB.
require "json"
require "uri"
require "pathname"
require "securerandom"
require "digest"
require "socket"
require "open3"
require "timeout"
raise "test required" unless ENV["RAILS_ENV"] == "test"
url = URI(ENV.fetch("DATABASE_URL"))
raise "isolated database required" unless url.host == "127.0.0.1" && url.path.match?(%r{\A/devdiary_rust_collector_interop_[0-9a-f]{32}\z})
root = Pathname.new(ENV.fetch("QUALIFICATION_ROOT"))
require File.join(Dir.pwd, "config/boot")
require_relative "qualification_database"
database_guard = QualificationDatabase.new(ENV.fetch("DATABASE_URL"))
database_guard.install!
require File.join(Dir.pwd, "config/environment")
# No rake schema task: it may enumerate extra configs or establish a new target.
# Validate the real resolver AND connected application in the same Ruby process.
database_guard.verify_application!
load File.join(Dir.pwd, "db/schema.rb")
database_guard.verify_application!
require "factory_bot_rails"
require "webmock"
require "puma"
WebMock.disable_net_connect!(allow_localhost: true)
Rails.logger = ActiveSupport::Logger.new(File::NULL)
FactoryBot.find_definitions if FactoryBot.factories.count.zero?
raise "database not empty" unless CollectorObservation.count.zero? && Workspace.count.zero?
scope = JSON.parse(root.join("scope.json").read)
actual = JSON.parse(root.join("observations.json").read)
raise "unexpected runtime rows" unless actual.size == 6 && actual.map { |r| r["event"] }.sort == %w[PostToolUse PreToolUse SessionEnd SessionStart Stop UserPromptSubmit].sort
endpoint = URI(scope.fetch("endpoint"))
raise "loopback required" unless endpoint.scheme == "http" && endpoint.host == "127.0.0.1"
Signal.trap("TERM") { raise Interrupt }
collector = server = proxy = proxy_thread = nil
key_path = root.join("disposable-collector.key")
report = {"passed" => false, "collector_revoked" => false}
begin
  workspace = FactoryBot.create(:workspace)
  repository = FactoryBot.create(:repository, full_name: "fixture/claude-qualification")
  WorkspaceRepository.create!(workspace: workspace, repository: repository, active: true)
  key = "dc_live_#{SecureRandom.urlsafe_base64(32)}"
  collector = CollectorCredential.create!(workspace: workspace, collector_ref: scope.fetch("collector_ref"), token_digest: Digest::SHA256.hexdigest(key))
  File.write(key_path, key, perm: 0o600)
  server = Puma::Server.new(Rails.application, nil, {min_threads: 0, max_threads: 2})
  server.add_tcp_listener("127.0.0.1", 0)
  port = server.binder.ios.first.addr[1]
  server.run
  proxy = TCPServer.new("127.0.0.1", endpoint.port)
  requests = []
  proxy_thread = Thread.new do
    (actual.size + 1).times do |i|
      downstream = upstream = nil
      begin
        Timeout.timeout(15) do
          downstream = proxy.accept
          upstream = TCPSocket.new("127.0.0.1", port)
          header = +""
          until header.end_with?("\r\n\r\n")
            chunk = downstream.read(1)
            raise "header bound" if chunk.nil? || header.bytesize >= 16_384
            header << chunk
          end
          length = header[/content-length: (\d+)/i, 1].to_i
          raise "body bound" unless length.between?(1, 16_384)
          body = downstream.read(length)
          raise "short body" unless body && body.bytesize == length
          requests << body
          header = header.gsub(/^connection:.*\r\n/i, "").sub("\r\n\r\n", "\r\nConnection: close\r\n\r\n")
          upstream.write(header + body)
          response = upstream.read
          downstream.write(response) unless i.zero?
        end
      ensure
        upstream&.close
        downstream&.close
      end
    end
  end
  command = lambda do |expected|
    # CLI itself caps every HTTP attempt at five seconds. Parent has a group deadline.
    out, err, status = Open3.capture3(root.join("collector").to_s, "sync", root.join("state").to_s, key_path.to_s, "100")
    raise "sync failure" unless status.exitstatus == expected
    raise "credential leak" if (out + err).include?(key)
    JSON.parse(out)
  end
  before = [WorkSession.count, SessionEvent.count, Attribution.count]
  first = command.call(1)
  raise "response-loss not committed" unless CollectorObservation.count == 1
  first_id = CollectorObservation.first.id
  delivered = command.call(0)
  raise "receipts incomplete" unless delivered == {"pending" => 0, "delivered" => actual.size}
  proxy_thread.join(5) || raise("proxy stuck")
  proxy_thread.value
  remote = CollectorObservation.order(:id).map(&:metadata)
  raise "payload mismatch" unless remote.sort_by { |r| r.fetch("observation_id") } == actual.sort_by { |r| r.fetch("observation_id") }
  raise "replay changed" unless requests.count(requests.first) == 2
  raise "duplicate record" unless CollectorObservation.count == actual.size && CollectorObservation.exists?(first_id)
  raise "actor invented" unless remote.all? { |r| r["attribution_basis"] == "unknown" && !r.key?("actor_ref") }
  raise "private metadata" if remote.to_json.match?(/PRIVATE_qualify|fixture\.txt|\/home\//)
  raise "attribution side effect" unless before == [WorkSession.count, SessionEvent.count, Attribution.count]
  expected_receipts = CollectorObservation.order(:id).map do |record|
    metadata = record.metadata
    {"collector_ref" => scope.fetch("collector_ref"), "observation_id" => metadata.fetch("observation_id"), "installation_id" => metadata.fetch("installation_id"), "record_id" => record.id}
  end
  root.join("expected-receipts.json").write(JSON.generate(expected_receipts))
  report.merge!("passed" => true, "actual_stock_records" => actual.size, "http_requests" => requests.size, "response_loss_result" => first, "final_status" => delivered, "payload_equality" => true, "exact_replay" => true, "no_attribution_side_effects" => true)
ensure
  # Every stage after credential creation is inside this ensure, including setup.
  begin
    if collector
      collector.update!(revoked_at: Time.current)
      raise "revocation failed" unless collector.reload.revoked_at && CollectorCredential.authenticate(key).nil?
      report["collector_revoked"] = true
    end
  ensure
    key_path.delete if key_path.exist?
    proxy&.close
    server&.stop(true)
    proxy_thread&.join(1)
    proxy_thread&.kill if proxy_thread&.alive?
    root.join("rails-result.json").write(JSON.pretty_generate(report))
  end
end
