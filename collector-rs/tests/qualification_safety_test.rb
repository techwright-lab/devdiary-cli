# frozen_string_literal: true

# Real ActiveRecord URL resolution, no Rails boot or foreign database connection.
require "minitest/autorun"
require "active_record"
require "active_record/database_configurations"
require "tmpdir"
require "open3"
require_relative "qualification_database"

class QualificationDatabaseTest < Minitest::Test
  OWNED = "postgresql://fixture@127.0.0.1:5432/devdiary_qualification_#{'a' * 32}"

  def setup
    @old = ENV["DATABASE_URL"]
    @old_rails = ENV["RAILS_ENV"]
    ENV["RAILS_ENV"] = "test"
    ENV["DATABASE_URL"] = OWNED
    @guard = QualificationDatabase.new(OWNED)
  end

  def teardown
    ENV["DATABASE_URL"] = @old
    ENV["RAILS_ENV"] = @old_rails
  end

  def resolve(value)
    ActiveRecord::DatabaseConfigurations.new("test" => value)
  end

  def test_real_resolver_explicit_url_beats_environment
    configs = resolve("url" => "postgresql://fixture@127.0.0.1:5432/unowned_fixture")
    assert_equal "unowned_fixture", configs.configs_for(env_name: "test").first.database
    assert_raises(RuntimeError) { @guard.verify_configurations!(configs) }
  end

  def test_only_exact_owned_single_target_is_allowed
    assert @guard.verify_configurations!(resolve("adapter" => "postgresql", "database" => "ignored"))
    [
      {"url" => OWNED.sub("127.0.0.1", "localhost")},
      {"url" => OWNED.sub(":5432", ":5433")},
      {"url" => OWNED + "?hostaddr=192.0.2.1"},
      {"url" => OWNED + "?service=foreign"},
      {"primary" => {"url" => OWNED}, "queue" => {"url" => OWNED, "database_tasks" => false}},
      {"primary" => {"url" => OWNED}, "replica" => {"url" => OWNED, "replica" => true}}
    ].each do |value|
      assert_raises(RuntimeError) { @guard.verify_configurations!(resolve(value)) }
    end
  end

  def test_actual_runner_rejects_competing_url_before_schema_or_network
    Dir.mktmpdir("qualification-target-") do |root|
      Dir.mkdir(File.join(root, "config"))
      Dir.mkdir(File.join(root, "db"))
      File.write(File.join(root, "config/boot.rb"), "")
      File.write(File.join(root, "config/environment.rb"), <<~RUBY)
        require "active_record"
        require "pg"
        def PG.connect(*) = raise("FOREIGN NETWORK ATTEMPT")
        ActiveRecord::Base.configurations = {"test" => {
          "url" => "postgresql://fixture@127.0.0.1:5432/unowned_fixture"
        }}
        ActiveRecord::Base.establish_connection(:test)
      RUBY
      marker = File.join(root, "schema-ran")
      File.write(File.join(root, "db/schema.rb"), "File.write(#{marker.inspect}, 'unsafe')")
      runner = File.join(__dir__, "qualify_rails.rb")
      code = 'file = ARGV.fetch(0); eval(File.read(file).split(%q{require "factory_bot_rails"}).first, TOPLEVEL_BINDING, file)'
      out, err, status = Open3.capture3({"DATABASE_URL" => OWNED, "RAILS_ENV" => "test", "QUALIFICATION_ROOT" => root},
        RbConfig.ruby, "-e", code, runner, chdir: root)
      refute status.success?
      assert_includes err, "unowned database target"
      refute_includes out + err, "FOREIGN NETWORK ATTEMPT"
      refute File.exist?(marker)
    end
  end

  def test_connection_override_rejected_before_pg_connect
    @guard.install!
    calls = []
    original = PG.method(:connect)
    PG.define_singleton_method(:connect) { |*| calls << :connect; raise "network forbidden" }
    assert_raises(RuntimeError) do
      ActiveRecord::ConnectionAdapters::PostgreSQLAdapter.new_client(
        host: "127.0.0.1", port: 5432, user: "fixture", dbname: "unowned_fixture"
      )
    end
    assert_empty calls
  ensure
    PG.define_singleton_method(:connect, original) if original
  end
end
