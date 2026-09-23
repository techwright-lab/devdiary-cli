# frozen_string_literal: true

require "minitest/autorun"
require "json"
require "ostruct"
require_relative "qualification_cleanup" if File.exist?(File.join(__dir__, "qualification_cleanup.rb"))

class CollectorCredential
  def self.authenticate(*) = nil
end
class Time
  def self.current = now
end

class QualificationCleanupTest < Minitest::Test
  STAGES = %w[revoke key_delete proxy_close server_stop thread_join thread_kill report_write].freeze

  # Execute the actual runner's ensure block, not a copied cleanup algorithm.
  def exercise(failures)
    calls = []
    written = nil
    action = ->(stage) do
      calls << stage
      raise IOError, "PRIVATE fault" if failures.include?(stage)
    end
    collector = Object.new
    collector.define_singleton_method(:update!) { |**| action.call("revoke") }
    collector.define_singleton_method(:reload) { OpenStruct.new(revoked_at: true) }
    key = "fixture"
    key_path = Object.new
    key_path.define_singleton_method(:exist?) { true }
    key_path.define_singleton_method(:delete) { action.call("key_delete") }
    proxy = Object.new
    proxy.define_singleton_method(:close) { action.call("proxy_close") }
    server = Object.new
    server.define_singleton_method(:stop) { |*| action.call("server_stop") }
    proxy_thread = Object.new
    proxy_thread.define_singleton_method(:join) { |*| action.call("thread_join") }
    proxy_thread.define_singleton_method(:alive?) { true }
    proxy_thread.define_singleton_method(:kill) { action.call("thread_kill") }
    writer = Object.new
    writer.define_singleton_method(:write) do |text|
      action.call("report_write")
      written = JSON.parse(text)
    end
    root = Object.new
    root.define_singleton_method(:join) { |*| writer }
    report = {"passed" => true, "collector_revoked" => false}
    source = File.read(File.join(__dir__, "qualify_rails.rb"))
    cleanup = "begin\n" + source[source.rindex("\nensure\n")..]
    raised = nil
    _out, err = capture_io do
      begin
        eval(cleanup, binding, "actual_cleanup")
      rescue Exception => e # includes interrupts intentionally, like outer harness
        raised = e
      end
    end
    [calls, report, written, raised, err]
  end

  def test_each_fault_still_attempts_all_later_cleanup_and_never_passes
    STAGES.each do |stage|
      calls, report, written, raised, err = exercise([stage])
      assert_equal STAGES, calls, stage
      refute report["passed"], stage
      refute_nil raised, stage
      assert_equal [stage], report["cleanup_failures"], stage
      if stage == "report_write"
        assert_equal false, JSON.parse(err)["passed"]
      else
        assert_equal false, written["passed"], stage
      end
      refute_includes err, "PRIVATE"
    end
  end

  def test_multiple_failures_are_aggregated
    calls, report, written, raised, = exercise(STAGES.take(6))
    assert_equal STAGES, calls
    assert_equal STAGES.take(6), report["cleanup_failures"]
    refute written["passed"]
    refute_nil raised
  end

  def test_success_is_preserved_only_when_cleanup_succeeds
    calls, report, written, raised, = exercise([])
    assert_equal STAGES, calls
    assert report["passed"]
    assert written["collector_revoked"]
    assert_nil raised
  end
end
