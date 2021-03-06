# coding=utf-8
# Copyright 2018 The Hypebot Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A basic IRC bot."""

# In general, we want to catch all exceptions, so ignore lint errors for e.g.
# catching Exception
# pylint: disable=broad-except

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from functools import partial
import re

from absl import app
from absl import flags
from absl import logging
import arrow

from hypebot import hypecore
from hypebot.commands import command_factory
from hypebot.core import params_lib
from hypebot.core import util_lib
from hypebot.interfaces import interface_factory
from hypebot.plugins import alias_lib
from hypebot.plugins import vegas_game_lib
from hypebot.protos.channel_pb2 import Channel

FLAGS = flags.FLAGS
flags.DEFINE_string('params', None, 'Bot parameter overrides.')


class BaseBot(object):
  """Class for shitposting in IRC."""

  DEFAULT_PARAMS = params_lib.HypeParams({
      # Human readable name to call bot. Will be normalized before most uses.
      'name': 'BaseBot',
      # Chat application interface.
      'interface': {'type': 'DiscordInterface'},
      # Restrict some responses to only these channels.
      'main_channels': ['.*'],
      # The default channel for announcements and discussion.
      'default_channel': {
          'id': '418098011445395462',
          'name': '#dev',
      },
      # Default time zone for display.
      'time_zone': 'America/Los_Angeles',
      'storage': {'type': 'RedisStore'},
      'stocks': {'type': 'IEXStock'},
      'execution_mode': {
          # If this bot is being run for development. Points to non-prod data
          # and changes the command prefix.
          'dev': True,
      },
      # List of commands for the bot to create.
      'commands': {
          'AliasAddCommand': {},
          'AliasCloneCommand': {},
          'AliasListCommand': {},
          'AliasRemoveCommand': {},
          'AskFutureCommand': {},
          'BuyHypeStackCommand': {},
          'CookieJarCommand': {},
          'DisappointCommand': {},
          'EchoCommand': {},
          'EnergyCommand': {},
          'GreetingPurchaseCommand': {},
          'GreetingsCommand': {},
          'GrepCommand': {},
          'HypeCommand': {},
          'HypeStackBalanceCommand': {},
          'InventoryList': {},
          'InventoryUse': {},
          'JackpotCommand': {},
          'KittiesSalesCommand': {},
          'MainCommand': {},
          'MemeCommand': {},
          'MissingPingCommand': {},
          'OrRiotCommand': {},
          'RageCommand': {},
          'RatelimitCommand': {},
          'RaiseCommand': {},
          'ReloadCommand': {},
          'RipCommand': {},
          'SameCommand': {},
          'SayCommand': {},
          'ScrabbleCommand': {},
          'SticksCommand': {},
          'StocksCommand': {},
          'StoryCommand': {},
          'SubCommand': {},
          'VersionCommand': {},
          'WordCountCommand': {},
          # Hypecoins
          'HCBalanceCommand': {},
          'HCBetCommand': {},
          'HCBetsCommand': {},
          'HCCirculationCommand': {},
          'HCForbesCommand': {},
          'HCGiftCommand': {},
          'HCResetCommand': {},
          'HCRobCommand': {},
          'HCTransactionsCommand': {},
          # Deployment
          'BuildCommand': {},
          'DeployCommand': {},
          'PushCommand': {},
          'SetSchemaCommand': {},
          'TestCommand': {},
          # Interface
          'JoinCommand': {},
          'LeaveCommand': {},
      },
      'version': '4.20.0'
  })

  def __init__(self, params):
    self._params = params_lib.HypeParams(self.DEFAULT_PARAMS)
    self._params.Override(params)
    if self._params.interface:
      self._params.interface.Override({'name': self._params.name.lower()})
    self._params.Lock()

    # self.interface always maintains the connected interface that is listening
    # for messages. self._core.interface holds the interface desired for
    # outgoing communication. It may be swapped out on the fly, e.g., to handle
    # nested callbacks.
    self.interface = interface_factory.CreateFromParams(self._params.interface)
    self._core = hypecore.Core(self._params, self.interface)

    self.interface.RegisterHandlers(self.HandleMessage, self._core.user_tracker)

    # TODO(someone): Factory built code change listener.

    # TODO(someone): Should betting on stocks be encapsulated into a
    # `command` so that it can be loaded on demand?
    self._stock_game = vegas_game_lib.StockGame(self._core.stocks)
    self._core.betting_games.append(self._stock_game)
    self._core.scheduler.DailyCallback(
        util_lib.ArrowTime(16, 0, 30, 'America/New_York'),
        self._StockCallback)

    self._commands = [
        command_factory.Create(name, params, self._core)
        for name, params in self._params.commands.AsDict().items()
        if params not in (None, False)
    ]

  def HandleMessage(self, channel, user, msg):
    """Handle an incoming message from the interface."""
    msg = self._ProcessAliases(channel, user, msg)
    msg = self._ProcessNestedCalls(channel, user, msg)

    if channel.visibility == Channel.PRIVATE:
      # See if someone is confirming/denying a pending request. This must happen
      # before command parsing so that we don't try to resolve a request created
      # in this message (e.g. !stack buy 1)
      if self._core.request_tracker.HasPendingRequest(user):
        self._core.request_tracker.ResolveRequest(user, msg)

    for command in self._commands:
      try:
        command.Handle(channel, user, msg)
      except Exception:
        self._core.Reply(user, 'Exception handling: %s' % msg,
                         log=True, log_level=logging.ERROR)

    # This must come after message processing for paychecks to work properly.
    self._core.executor.submit(self._RecordUserActivity, channel, user)

  def _ProcessAliases(self, unused_channel, user, msg):
    return alias_lib.ExpandAliases(self._core.cached_store, user, msg)

  NESTED_PATTERN = re.compile(r'\$\(([^\(\)]+'
                              # TODO(someone): Actually tokenize input
                              # instead of relying on cheap hacks
                              r'(?:[^\(\)]*".*?"[^\(\)]*)*)\)')

  def _ProcessNestedCalls(self, channel, user, msg):
    """Evaluate nested commands within $(...)."""
    m = self.NESTED_PATTERN.search(msg)
    while m:
      backup_interface = self._core.interface
      self._core.interface = interface_factory.Create('CaptureInterface', {})

      # Pretend it's Private to avoid ratelimit.
      nested_channel = Channel(id=channel.id,
                               visibility=Channel.PRIVATE,
                               name=channel.name)
      self.HandleMessage(nested_channel, user, m.group(1))
      response = self._core.interface.MessageLog()

      msg = msg[:m.start()] + response + msg[m.end():]
      self._core.interface = backup_interface
      m = self.NESTED_PATTERN.search(msg)
    return msg

  def _RecordUserActivity(self, unused_channel, user):
    """TLA recording of users."""
    if not self._core.user_tracker.IsKnown(user):
      logging.info('Unknown user %s, not recording activity', user)
      self.interface.Who(user)
      return
    if self._core.user_tracker.IsBot(user) or not self._core.cached_store:
      return
    # Update user's activity. We only store at the 5 minute resolution to cut
    # down on the number of storage writes we need to do.
    utc_now = arrow.utcnow().timestamp // (5 * 60)
    try:
      self._core.cached_store.SetValue(user, 'lastseen', str(utc_now))
    except Exception as e:
      logging.error('Error recording user activity: %s', e)

  def _StockCallback(self):
    msg_fn = partial(self._core.Reply,
                     default_channel=self._core.default_channel)
    self._core.bets.SettleBets(self._stock_game, self._core.nick, msg_fn)


def main(argv):
  if len(argv) > 1:
    raise app.UsageError('Too many command-line arguments.')
  bot = BaseBot(FLAGS.params)
  bot.interface.Loop()


if __name__ == '__main__':
  app.run(main)
