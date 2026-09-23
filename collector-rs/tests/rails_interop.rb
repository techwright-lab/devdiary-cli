# frozen_string_literal: true

# Opt-in actual Rails/Puma/PostgreSQL interoperability, never live credentials.
# Run from a disposable Rails checkout with test-only DATABASE_URL (README).
require "pathname"
require "uri"
raise "test environment required" unless ENV["RAILS_ENV"] == "test"
url = URI(ENV.fetch("DATABASE_URL"))
raise "disposable local database required" unless %w[127.0.0.1 localhost].include?(url.host) && url.path.start_with?("/devdiary_rust_collector_interop_")
cli = Pathname.new(__dir__).parent
require File.join(Dir.pwd, "config/environment")
require "factory_bot_rails"
require "webmock"
require "puma"
require "socket"
require "open3"
require "tmpdir"
require "timeout"
WebMock.disable_net_connect!(allow_localhost: true)
Rails.logger = ActiveSupport::Logger.new(File::NULL)
FactoryBot.find_definitions if FactoryBot.factories.count.zero?
raise "database must be empty" unless CollectorObservation.count.zero? && Workspace.count.zero?
workspace = FactoryBot.create(:workspace)
repository = FactoryBot.create(:repository)
WorkspaceRepository.create!(workspace: workspace, repository: repository, active: true)
collector, key = CollectorCredential.issue!(workspace: workspace)
server = Puma::Server.new(Rails.application, nil, {min_threads: 0, max_threads: 2})
server.add_tcp_listener("127.0.0.1", 0)
port = server.binder.ios.first.addr[1]
server.run
proxy = TCPServer.new("127.0.0.1", 0)
requests = []
# Deliver through the actual HTTP stack, then discard the first response after
# PostgreSQL commit. Subsequent deliveries must retrieve that same record.
proxy_thread = Thread.new do
  3.times do |i|
    downstream = proxy.accept
    upstream = TCPSocket.new("127.0.0.1", port)
    header = +""
    header << downstream.read(1) until header.end_with?("\r\n\r\n")
    length = header[/content-length: (\d+)/i, 1].to_i
    raise "unbounded body" unless length.between?(1, 16_384)
    body = downstream.read(length)
    requests << body
    header = header.gsub(/^connection:.*\r\n/i, "").sub("\r\n\r\n", "\r\nConnection: close\r\n\r\n")
    upstream.write(header + body)
    response = upstream.read
    downstream.write(response) unless i.zero?
  ensure
    upstream&.close
    downstream&.close
  end
end
begin
  Dir.mktmpdir("rust-rails-interop-") do |dir|
    state = File.join(dir, "state")
    Dir.mkdir(state, 0o700)
    key_path = File.join(dir, "key")
    File.write(key_path, key, perm: 0o600)
    config = JSON.parse(File.read(cli.join("tests/fixtures/scope.json"))).merge(
      "endpoint" => "http://127.0.0.1:#{proxy.addr[1]}/ingest/v1/observations",
      "collector_ref" => collector.collector_ref,
      "repository_ref" => "https://github.com/#{repository.full_name}"
    )
    row = JSON.parse(File.read(cli.join("tests/fixtures/local-observation.json")))
    command = lambda do |verb, args = [], input = "", expected = 0|
      out, err, status = Timeout.timeout(10) do
        Open3.capture3(cli.join("target/debug/devdiary-collector").to_s, verb, state, *args, stdin_data: input)
      end
      raise "collector failed: #{verb}: #{status.exitstatus}" unless status.exitstatus == expected
      raise "private diagnostic" if (out + err).include?(key) || (out + err).include?("PRIVATE-SENTINEL")
      JSON.parse(out)
    end
    command.call("init", ["--consent"], config.to_json)
    command.call("collect", [], row.to_json)
    before = [WorkSession.count, SessionEvent.count, Attribution.count]
    command.call("sync", [key_path, "1"], "", 1)
    raise "server did not commit" unless CollectorObservation.count == 1
    first = CollectorObservation.first
    metadata = first.metadata
    raise "private metadata" if metadata.to_json.include?("PRIVATE-SENTINEL")
    raise "unknown actor lost" unless metadata["attribution_basis"] == "unknown" && !metadata.key?("actor_ref")
    raise "vendor identities lost" unless metadata.values_at("turn_id", "agent_id", "tool_use_id") == %w[turn-1 child-1 tool-1]
    result = command.call("sync", [key_path, "1"])
    raise "not acknowledged" unless result == {"pending" => 0, "delivered" => 1}
    raise "duplicate remote record" unless CollectorObservation.count == 1 && CollectorObservation.first.id == first.id
    raise "retry changed bytes" unless requests[0] == requests[1]
    raise "metadata mismatch" unless JSON.parse(requests[0]) == metadata
    collector.update!(revoked_at: Time.current)
    row["observation_id"] = "33333333-3333-4333-8333-333333333333"
    command.call("collect", [], row.to_json)
    result = command.call("sync", [key_path, "1"], "", 1)
    raise "revocation not retained" unless result == {"pending" => 1, "delivered" => 1} && CollectorObservation.count == 1
    raise "attribution side effect" unless before == [WorkSession.count, SessionEvent.count, Attribution.count]
    puts "PASS: Rust -> Rails HTTP/PostgreSQL; response-loss exact replay; one record; exact receipt; revocation; unknown actor/vendor IDs; no attribution side effects"
  end
ensure
  proxy.close
  server.stop(true)
  proxy_thread.join(3)
  proxy_thread.kill if proxy_thread.alive?
end
