# frozen_string_literal: true

require "pty"
require "io/wait"
require "uri"

# `devdiary setup` on a terminal against a Rails server in this process, for the
# Rails harnesses. The terminal half needs only Ruby. `approve` and `serve` need
# Net::HTTP, Nokogiri and Puma, which the caller requires after Rails boots.
module RailsSetup
  QUESTION = "[y/N]"
  CODE = /Approval code: ([A-Z]{4}-[A-Z]{4})/
  CONNECTED = "Connected. Return to the terminal."
  SCHEDULERS = %w[DEVDIARY_COLLECTOR_SYSTEMCTL DEVDIARY_COLLECTOR_LAUNCHCTL].freeze

  # Setup writes hooks under HOME, job definitions under XDG_CONFIG_HOME or HOME,
  # and starts the job with the named fake. Refuses unless all of them are in
  # `root`. Only a debug build reads the fake; the parent runs no other build.
  def self.isolated!(root, env = ENV)
    inside = ->(key) { env[key].to_s.start_with?("#{root}/") }
    fakes = SCHEDULERS.select { |key| env.key?(key) }
    return if inside.call("HOME") && inside.call("XDG_CONFIG_HOME") && fakes.size == 1 && inside.call(fakes.first)

    raise "temporary HOME and fake scheduler required"
  end

  # Runs `argv` on a new terminal. Answers each [y/N] question in turn from
  # `answers`, and "n" after them. Yields the approval code once, while setup
  # waits for it. Returns [exit status, everything the terminal showed].
  def self.terminal(argv, env: {}, answers: [], timeout: 60)
    deadline = now + timeout
    output = +""
    asked = 0
    code = nil
    reader, writer, pid = PTY.spawn(env, *argv)
    begin
      loop do
        left = deadline - now
        raise "setup timed out" if left <= 0
        next unless reader.wait_readable([left, 0.2].min)

        begin
          output << reader.readpartial(4096)
        rescue EOFError, Errno::EIO
          break
        end
        raise "setup output bound" if output.bytesize > 1_000_000

        while output.scan(QUESTION).size > asked
          writer.write("#{answers.fetch(asked, "n")}\n")
          asked += 1
        end
        if code.nil? && (code = output[CODE, 1])
          yield code if block_given?
        end
      end
      status = reap(pid, deadline)
      pid = nil
      [status, output]
    ensure
      stop(pid) if pid
      reader.close
      writer.close
    end
  end

  # Approves `code` through the real approval page, as the signed-in person
  # would. A POST without the page's CSRF token must be refused first. Then it
  # follows the redirect to the CLI's callback, as the browser does. Returns the
  # verifier and whether the CLI's page said it connected.
  def self.approve(http, code, workspace_id:)
    page = http.get("/collector/approve?code=#{code}")
    raise "approval page #{page.code}" unless page.code == "200"

    doc = Nokogiri::HTML(page.body)
    form = doc.at_css('form[action="/collector/approve"][method="post"]') || raise("approval form missing")
    token = form.at_css('input[name="authenticity_token"]')&.[]("value") ||
      doc.at_css('meta[name="csrf-token"]')&.[]("content") || raise("real CSRF missing")
    post = Net::HTTP::Post.new("/collector/approve")
    post["Cookie"] = page.get_fields("set-cookie").map { |c| c.split(";", 2).first }.join("; ")
    values = {code: form.at_css('input[name="code"]')["value"], workspace_id:, bind_interactive: "1"}
    post.set_form_data(values)
    denied = http.request(post)
    raise "CSRF bypassed #{denied.code}" unless denied.code == "422"

    post.set_form_data(values.merge(authenticity_token: token))
    approved = http.request(post)
    raise "approval #{approved.code}" unless approved.code == "303"

    callback = URI(approved["location"])
    raise "callback off loopback" unless callback.scheme == "http" && callback.host == "127.0.0.1" && callback.path == "/callback"

    answer = Net::HTTP.get_response(callback)
    [URI.decode_www_form(callback.query).to_h.fetch("verifier"), answer.code == "200" && answer.body.include?(CONNECTED)]
  end

  # Puma on 127.0.0.1, on `port` when given. Returns [server, port].
  def self.serve(app, port = 0)
    server = Puma::Server.new(app, nil, {min_threads: 0, max_threads: 4})
    server.add_tcp_listener("127.0.0.1", port)
    server.run
    [server, server.binder.ios.first.addr[1]]
  end

  def self.now = Process.clock_gettime(Process::CLOCK_MONOTONIC)

  def self.reap(pid, deadline)
    loop do
      _, status = Process.waitpid2(pid, Process::WNOHANG)
      return status.exitstatus if status
      raise "setup timed out" if now > deadline

      sleep 0.05
    end
  end

  def self.stop(pid)
    %w[TERM KILL].each do |signal|
      Process.kill(signal, pid)
      20.times do
        return if Process.waitpid(pid, Process::WNOHANG)

        sleep 0.05
      end
    end
  rescue Errno::ESRCH, Errno::ECHILD
    nil
  end
  private_class_method :now, :reap, :stop
end
