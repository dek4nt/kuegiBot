import traceback
from datetime import datetime
from time import sleep
from typing import List
import time

import plotly.graph_objects as go

from kuegi_bot.bitmex.bitmex_interface import BitmexInterface
from kuegi_bot.bybit.bybit_interface import ByBitInterface
from kuegi_bot.utils import log, errors
from kuegi_bot.bots.trading_bot import TradingBot
from kuegi_bot.utils.trading_classes import OrderInterface, Order, Account, Bar, Symbol, ExchangeInterface


class LiveTrading(OrderInterface):

    def __init__(self, settings, trading_bot: TradingBot):
        self.settings = settings
        self.id = self.settings.id
        self.last_tick= 0

        self.logger = log.setup_custom_logger(name=settings.id,
                                              log_level=settings.LOG_LEVEL,
                                              logToConsole=settings.LOG_TO_CONSOLE,
                                              logToFile=settings.LOG_TO_FILE)
        self.exchange: ExchangeInterface = None
        if settings.EXCHANGE == 'bitmex':
            self.exchange = BitmexInterface(settings=settings, logger=self.logger,on_tick_callback=self.on_tick)
        else:
            self.exchange = ByBitInterface(settings=settings, logger=self.logger,on_tick_callback=self.on_tick)

        # Once exchange is created, register exit handler that will always cancel orders
        # on any error.
        self.alive = True

        if self.exchange.is_open():
            self.logger.info("############# Starting Live Trading Engine for %s ##############" % self.exchange.symbol)
            self.symbolInfo: Symbol = self.exchange.get_instrument()
            self.bot: TradingBot = trading_bot
            self.bot.prepare(self.logger,self)
            # init market data dict to be filled later
            self.bars: List[Bar] = []
            self.update_bars()
            self.account: Account = Account()
            self.update_account()
            self.bot.reset()
            self.bot.init(bars=self.bars, account=self.account, symbol=self.symbolInfo, unique_id=self.settings.id)
        else:
            self.alive = False

    def on_tick(self):
        self.last_tick= time.time()

    def print_status(self):
        """Print the current status."""
        self.logger.info("Current Contract Position: %d" % self.exchange.get_position())
        """TODO: open orders"""

    ###
    # Order handling
    ###

    def send_order(self, order: Order):
        self.exchange.send_order(order)

    def update_order(self, order: Order):
        self.exchange.update_order(order)

    def cancel_order(self, orderId):
        self.exchange.cancel_order(orderId)

    ###
    # Sanity
    ##

    def sanity_check(self):
        """Perform checks before placing orders."""
        # Ensure market is still open.
        self.exchange.check_market_open()

    ###
    # Running
    ###

    def update_account(self):
        self.exchange.update_account(self.account)
        orders = self.exchange.get_orders()
        prevOpenIds = []
        for o in self.account.open_orders:
            prevOpenIds.append(o.id)

        self.account.open_orders = []
        for o in orders:
            if o.active:
                self.account.open_orders.append(o)
            elif len(o.id) > 0 and o.id in prevOpenIds:
                self.logger.info(
                    "order %s got %s @ %s" % (
                        o.id,
                        ("executed" if o.executed_amount != 0 else "canceled"),
                        ("%.1f" % o.executed_price) if o.executed_price is not None else None))
                self.account.order_history.append(o)

    def update_bars(self):
        """get data from exchange"""
        if len(self.bars) < 10:
            self.bars = self.exchange.get_bars(self.settings.MINUTES_PER_BAR, 0)
        else:
            new_bars = self.exchange.recent_bars(self.settings.MINUTES_PER_BAR, 0)
            for b in reversed(new_bars):
                if b.tstamp < self.bars[0].tstamp:
                    continue
                elif b.tstamp == self.bars[0].tstamp:
                    # merge?
                    if b.subbars[-1].tstamp == self.bars[0].subbars[-1].tstamp:
                        self.bars[0] = b
                    else:
                        # merge!
                        first = self.bars[0].subbars[-1]
                        newBar = Bar(tstamp=b.tstamp, open=first.open, high=first.high, low=first.low,
                                     close=first.close,
                                     volume=first.volume, subbars=[first])
                        for sub in reversed(self.bars[0].subbars[:-1]):
                            if sub.tstamp < b.subbars[-1].tstamp:
                                newBar.add_subbar(sub)
                            else:
                                break
                        for sub in reversed(b.subbars):
                            if sub.tstamp > newBar.subbars[0].tstamp:
                                newBar.add_subbar(sub)
                            else:
                                continue
                        self.bars[0] = newBar
                else:  # b.tstamp > self.bars[0].tstamp
                    self.bars.insert(0, b)

    def check_connection(self):
        """Ensure the WS connections are still open."""
        return self.exchange.is_open()

    def exit(self):
        if not self.alive:
            return
        self.logger.info("Shutting down. open orders are not touched! Close manually!")
        try:
            self.exchange.exit()
        except errors.AuthenticationError as e:
            self.logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            self.logger.info("Unable to exit exchange: %s" % e)
        self.alive = False

    def handle_tick(self):
        try:
            self.update_bars()
            self.update_account()
            self.bot.on_tick(self.bars, self.account)
            for bar in self.bars:
                bar.did_change = False
        except Exception as e:
            self.logger.error("Exception in handle_tick: " + traceback.format_exc())
            raise e

    def run_loop(self):
        last = 0
        while self.alive:
            current= time.time()
            # execute if last execution is to long ago
            # or there was a tick since the last execution but the tick is more than debounce ms ago (to prevent race condition of account updates etc.)
            if current - last > self.settings.LOOP_INTERVAL or (last < self.last_tick < current - 2):
                last= time.time()
                if not self.check_connection():
                    self.logger.error("Realtime data connection unexpectedly closed, exiting.")
                    self.exit()
                else:
                    self.handle_tick()

            sleep(0.5)

    def prepare_plot(self):
        self.logger.info("running timelines")
        time = list(map(lambda b: datetime.fromtimestamp(b.tstamp), self.bars))
        open = list(map(lambda b: b.open, self.bars))
        high = list(map(lambda b: b.high, self.bars))
        low = list(map(lambda b: b.low, self.bars))
        close = list(map(lambda b: b.close, self.bars))

        self.logger.info("creating plot")
        fig = go.Figure(data=[go.Candlestick(x=time, open=open, high=high, low=low, close=close, name="XBTUSD")])

        self.logger.info("adding bot data")
        self.bot.add_to_plot(fig, self.bars, time)

        fig.update_layout(xaxis_rangeslider_visible=False)
        return fig
