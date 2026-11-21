# frozen_string_literal: true

# The terminal side of RailsSetup against fake setup scripts: no Rails, database or collector.
require "minitest/autorun"
require "tmpdir"
require_relative "rails_setup"

class RailsSetupTerminalTest < Minitest::Test
  def setup
    @dir = Dir.mktmpdir("rails-setup-")
  end

  def teardown
    FileUtils.remove_entry(@dir)
  end

  def fake(body)
    path = File.join(@dir, "setup")
    File.write(path, "#!/bin/sh\n#{body}\n")
    File.chmod(0o700, path)
    path
  end

  def test_answers_each_question_yields_the_code_once_and_returns_the_status
    exe = fake(<<~SH)
      printf 'Append these entries? [y/N] '; read a; echo "first:$a"
      echo 'Approval code: BCDF-GHJK'
      echo 'Approval code: BCDF-GHJK'
      printf 'Approve these names? [y/N] '; read b; echo "second:$b"
      exit 3
    SH
    codes = []
    status, output = RailsSetup.terminal([exe], answers: %w[y y]) { |code| codes << code }
    assert_equal 3, status
    assert_equal ["BCDF-GHJK"], codes
    assert_includes output, "first:y"
    assert_includes output, "second:y"
  end

  def test_questions_past_the_answers_are_declined
    exe = fake(%(printf 'Append? [y/N] '; read a; echo "answer:$a"))
    status, output = RailsSetup.terminal([exe])
    assert_equal 0, status
    assert_includes output, "answer:n"
  end

  def test_the_environment_reaches_setup
    exe = fake(%(echo "home:$HOME"))
    _, output = RailsSetup.terminal([exe], env: {"HOME" => @dir})
    assert_includes output, "home:#{@dir}"
  end

  def test_setup_may_write_only_inside_the_run
    inside = {"HOME" => "#{@dir}/home", "XDG_CONFIG_HOME" => "#{@dir}/config",
              "DEVDIARY_COLLECTOR_SYSTEMCTL" => "#{@dir}/bin/systemctl"}
    assert_nil RailsSetup.isolated!(@dir, inside)
    [
      inside.merge("HOME" => Dir.home),
      inside.merge("XDG_CONFIG_HOME" => "#{@dir}-other/config"),
      inside.except("DEVDIARY_COLLECTOR_SYSTEMCTL"),
      inside.merge("DEVDIARY_COLLECTOR_LAUNCHCTL" => "#{@dir}/bin/launchctl"),
      inside.merge("DEVDIARY_COLLECTOR_SYSTEMCTL" => "/usr/bin/systemctl")
    ].each do |env|
      assert_raises(RuntimeError) { RailsSetup.isolated!(@dir, env) }
    end
  end

  def test_a_setup_that_never_ends_is_stopped_at_the_deadline
    exe = fake("sleep 30")
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    error = assert_raises(RuntimeError) { RailsSetup.terminal([exe], timeout: 1) }
    assert_equal "setup timed out", error.message
    assert_operator Process.clock_gettime(Process::CLOCK_MONOTONIC) - started, :<, 5
  end
end
