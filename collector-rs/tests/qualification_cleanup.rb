# frozen_string_literal: true

require "json"

module QualificationCleanup
  def self.run(report, actions, writer)
    failures = []
    actions.each do |stage, action|
      begin
        action.call
      rescue StandardError, Interrupt
        failures << stage
      end
    end
    report["cleanup_failures"] = failures
    report["passed"] = false unless failures.empty?
    begin
      writer.call(JSON.pretty_generate(report))
    rescue StandardError, Interrupt
      failures << "report_write"
      report["passed"] = false
      # Parent cannot trust a partial file. Also emit a sanitized failed report;
      # the nonzero child exit and parent-owned DB drop remain authoritative.
      warn JSON.generate(report)
    end
    raise "qualification cleanup failed" unless failures.empty?
  end
end
