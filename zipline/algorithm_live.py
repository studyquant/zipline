#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from datetime import time
import os.path
import copy

import logbook
import pandas as pd

from zipline.finance.performance import PerformanceTracker
from zipline.finance.trading import SimulationParameters
from zipline.finance.blotter_live import BlotterLive
from zipline.algorithm import TradingAlgorithm
from zipline.gens.realtimeclock import RealtimeClock
from zipline.gens.tradesimulation import AlgorithmSimulator
from zipline.errors import ScheduleFunctionOutsideTradingStart
from zipline.utils.api_support import (
    ZiplineAPI,
    api_method,
    allowed_only_in_before_trading_start)

from zipline.utils.calendars.trading_calendar import days_at_time
from zipline.utils.serialization_utils import load_context, store_context

log = logbook.Logger("Live Trading")


class LiveAlgorithmExecutor(AlgorithmSimulator):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)

    def _cleanup_expired_assets(self, dt, position_assets):
        # This method is invoked in simulation to clean up assets & orders
        # which passed auto_close_date. In live trading we allow assets
        # traded after auto_close_date (which is set to last ingestion + 1d)
        # for one reason: Not all algorithms use historical data and for those
        # continuous (daily) ingestion is not needed.
        pass


class LiveTradingAlgorithm(TradingAlgorithm):
    def __init__(self, *args, **kwargs):
        self.broker = kwargs.pop('broker', None)
        self.orders = {}

        self.algo_filename = kwargs.get('algo_filename', "<algorithm>")
        self.state_filename = kwargs.pop('state_filename', None)
        self.realtime_bar_target = kwargs.pop('realtime_bar_target', None)
        self._context_persistence_blacklist = ['trading_client']
        self._context_persistence_whitelist = ['initialized', 'perf_tracker']
        self._context_persistence_excludes = []

        if 'blotter' not in kwargs:
            blotter_live = BlotterLive(
                data_frequency=kwargs['sim_params'].data_frequency,
                broker=self.broker)
            kwargs['blotter'] = blotter_live

        super(self.__class__, self).__init__(*args, **kwargs)

        log.info("initialization done")

    def initialize(self, *args, **kwargs):
        self._context_persistence_excludes = \
            self._context_persistence_blacklist + \
            [e for e in self.__dict__.keys()
             if e not in self._context_persistence_whitelist]

        if os.path.isfile(self.state_filename):
            log.info("Loading state from {}".format(self.state_filename))

            perf_tracker_before_restore = copy.copy(self.perf_tracker)
            load_context(self.state_filename,
                         context=self,
                         checksum=self.algo_filename)
            perf_tracker_after_restore = self.perf_tracker

            if perf_tracker_after_restore and perf_tracker_before_restore:
                # Extend yesterday's sim_params and perf_tracker to track
                # today's session.
                yesterday = perf_tracker_after_restore
                today = perf_tracker_before_restore

                self.trading_calendar = today.trading_calendar
                self.sim_params = SimulationParameters(
                    start_session=yesterday.sim_params.start_session,
                    end_session=today.sim_params.end_session,
                    trading_calendar=self.trading_calendar,
                    capital_base=yesterday.sim_params.capital_base,
                    emission_rate=yesterday.sim_params.emission_rate,
                    data_frequency=yesterday.sim_params.data_frequency,
                    arena=yesterday.sim_params.arena
                )
                self.perf_tracker = yesterday.new_with_params(
                    self.sim_params, self.trading_environment,
                    self.trading_calendar, self.data_portal)

            return

        with ZiplineAPI(self):
            super(self.__class__, self).initialize(*args, **kwargs)
            store_context(self.state_filename,
                          context=self,
                          checksum=self.algo_filename,
                          exclude_list=self._context_persistence_excludes)

    def handle_data(self, data):
        super(self.__class__, self).handle_data(data)
        store_context(self.state_filename,
                      context=self,
                      checksum=self.algo_filename,
                      exclude_list=self._context_persistence_excludes)

    def _create_clock(self):
        # This method is taken from TradingAlgorithm.
        # The clock has been replaced to use RealtimeClock
        trading_o_and_c = self.trading_calendar.schedule.ix[
            self.sim_params.sessions]
        assert self.sim_params.emission_rate == 'minute'

        minutely_emission = True
        market_opens = trading_o_and_c['market_open']
        market_closes = trading_o_and_c['market_close']

        # The calendar's execution times are the minutes over which we actually
        # want to run the clock. Typically the execution times simply adhere to
        # the market open and close times. In the case of the futures calendar,
        # for example, we only want to simulate over a subset of the full 24
        # hour calendar, so the execution times dictate a market open time of
        # 6:31am US/Eastern and a close of 5:00pm US/Eastern.
        execution_opens = \
            self.trading_calendar.execution_time_from_open(market_opens)
        execution_closes = \
            self.trading_calendar.execution_time_from_close(market_closes)

        before_trading_start_minutes = ((pd.to_datetime(execution_opens.values)
            .tz_localize('UTC')
            .tz_convert('US/Eastern')+
            timedelta(minutes=-_minutes_before_trading_starts))
            .tz_convert('UTC')
            )

        return RealtimeClock(
            self.sim_params.sessions,
            execution_opens,
            execution_closes,
            before_trading_start_minutes,
            minute_emission=minutely_emission,
            time_skew=self.broker.time_skew,
            is_broker_alive=self.broker.is_alive
        )

    def _create_generator(self, sim_params):
        if sim_params is not None:
            self.sim_params = sim_params

        if self.perf_tracker is None:
            # HACK: When running with the `run` method, we set perf_tracker to
            # None so that it will be overwritten here.
            self.perf_tracker = PerformanceTracker(
                sim_params=self.sim_params,
                trading_calendar=self.trading_calendar,
                env=self.trading_environment,
            )

            # Set the dt initially to the period start by forcing it to change.
            self.on_dt_changed(self.sim_params.start_session)

        if not self.initialized:
            self.initialize(*self.initialize_args, **self.initialize_kwargs)
            self.initialized = True

        self.trading_client = LiveAlgorithmExecutor(
            self,
            self.sim_params,
            self.data_portal,
            self._create_clock(),
            self._create_benchmark_source(),
            self.restrictions,
            universe_func=self._calculate_universe
        )

        return self.trading_client.transform()

    def updated_portfolio(self):
        return self.broker.portfolio

    def updated_account(self):
        return self.broker.account

    @api_method
    @allowed_only_in_before_trading_start(
        ScheduleFunctionOutsideTradingStart())
    def schedule_function(self,
                          func,
                          date_rule=None,
                          time_rule=None,
                          half_days=True,
                          calendar=None):
        # If the scheduled_function() is called from initalize()
        # then the state persistence would need to take care of storing and
        # restoring the scheduled functions too (as initialize() only called
        # once in the algorithm's life). Persisting scheduled functions are
        # difficult as they are not serializable by default.
        # We enforce scheduled functions to be called only from
        # before_trading_start() in live trading with a decorator.
        super(self.__class__, self).schedule_function(func,
                                                      date_rule,
                                                      time_rule,
                                                      half_days,
                                                      calendar)

    @api_method
    def symbol(self, symbol_str):
        # This method works around the problem of not being able to trade
        # assets which does not have ingested data for the day of trade.
        # Normally historical data is loaded to bundle and the asset's
        # end_date and auto_close_date is set based on the last entry from
        # the bundle db. LiveTradingAlgorithm does not override order_value(),
        # order_percent() & order_target(). Those higher level ordering
        # functions provide a safety net to not to trade de-listed assets.
        # If the asset is returned as it was ingested (end_date=yesterday)
        # then CannotOrderDelistedAsset exception will be raised from the
        # higher level order functions.
        #
        # Hence, we are increasing the asset's end_date by 10,000 days.
        # The ample buffer is provided for two reasons:
        # 1) assets are often stored in algo's context through initialize(),
        #    which is called once and persisted at live trading. 10,000 days
        #    enables 27+ years of trading, which is more than enough.
        # 2) Tool - 10,000 Days is brilliant!

        asset = super(self.__class__, self).symbol(symbol_str)
        tradeable_asset = asset.to_dict()
        tradeable_asset['end_date'] = (pd.Timestamp('now', tz='UTC') +
                                       pd.Timedelta('10000 days'))
        tradeable_asset['auto_close_date'] = tradeable_asset['end_date']
        self.broker.subscribe_to_market_data(asset)
        return asset.from_dict(tradeable_asset)

    def run(self, *args, **kwargs):
        daily_stats = super(self.__class__, self).run(*args, **kwargs)
        self.on_exit()
        return daily_stats

    def on_exit(self):
        if not self.realtime_bar_target:
            return

        log.info("Storing realtime bars to: {}".format(
            self.realtime_bar_target))

        today = str(pd.to_datetime('today').date())
        subscribed_assets = self.broker.subscribed_assets
        realtime_history = self.broker.get_realtime_bars(subscribed_assets,
                                                         '1m')

        if not os.path.exists(self.realtime_bar_target):
            os.mkdir(self.realtime_bar_target)

        for asset in subscribed_assets:
            filename = "ZL-%s-%s.csv" % (asset.symbol, today)
            path = os.path.join(self.realtime_bar_target, filename)
            realtime_history[asset].to_csv(path, mode='a',
                                           index_label='datetime',
                                           header=not os.path.exists(path))
