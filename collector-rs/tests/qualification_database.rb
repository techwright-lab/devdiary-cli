# frozen_string_literal: true

require "uri"
require "active_record"
require "active_record/connection_adapters/postgresql_adapter"

# Single-process guard: install before application boot, retain through schema
# load and fixtures. Reviewed checkout code is trusted, its DB routing is not.
class QualificationDatabase
  CONFIG_KEYS = %i[adapter database host port username password encoding pool
    connect_timeout prepared_statements advisory_locks min_messages].freeze
  CLIENT_KEYS = %i[dbname host port user password connect_timeout].freeze

  def initialize(url)
    uri = URI(url)
    raise "isolated database required" unless uri.scheme == "postgresql" &&
      uri.host == "127.0.0.1" && uri.port && uri.user&.match?(/\A[a-zA-Z0-9]+\z/) &&
      uri.path.match?(%r{\A/devdiary_rust_collector_interop_[0-9a-f]{32}\z}) &&
      !uri.password && !uri.query && !uri.fragment
    @expected = {host: uri.host, port: uri.port, database: uri.path.delete_prefix("/"), username: uri.user}.freeze
  end

  def verify_target!(config)
    raise "unowned database target" unless config[:adapter] == "postgresql" &&
      config[:host] == @expected[:host] && config[:port].to_s == @expected[:port].to_s &&
      config[:database] == @expected[:database] && config[:username] == @expected[:username]
    raise "database routing overrides refused" unless (config.keys - CONFIG_KEYS).empty?
    true
  end

  def verify_configurations!(configurations)
    configs = configurations.configs_for(env_name: "test", include_hidden: true)
    raise "single database required" unless configs.size == 1
    verify_target!(configs.first.configuration_hash)
  end

  def verify_client!(params)
    raise "database client overrides refused" unless params.is_a?(Hash) && (params.keys - CLIENT_KEYS).empty?
    verify_target!(adapter: "postgresql", database: params[:dbname], host: params[:host],
      port: params[:port], username: params[:user])
  end

  def install!
    guard = self
    ActiveRecord::ConnectionAdapters::PostgreSQLAdapter.singleton_class.prepend(Module.new do
      define_method(:new_client) do |params|
        guard.verify_client!(params)
        super(params)
      end
    end)
  end

  def verify_application!
    verify_configurations!(ActiveRecord::Base.configurations)
    ActiveRecord::Base.connection_handler.connection_pool_list(:all).each do |pool|
      verify_target!(pool.db_config.configuration_hash)
    end
    ActiveRecord::Base.with_connection do |connection|
      verify_target!(connection.pool.db_config.configuration_hash)
      raw = connection.raw_connection
      raise "connected database mismatch" unless raw.host == @expected[:host] &&
        raw.port == @expected[:port] && raw.db == @expected[:database] && raw.user == @expected[:username]
      raise "server database mismatch" unless connection.select_value("SELECT current_database()") == @expected[:database]
    end
    true
  end
end
