#####################################################################################
#
#  Copyright (c) Crossbar.io Technologies GmbH
#
#  Unless a separate license agreement exists between you and Crossbar.io GmbH (e.g.
#  you have purchased a commercial license), the license terms below apply.
#
#  Should you enter into a separate license agreement after having received a copy of
#  this software, then the terms of such license agreement replace the terms below at
#  the time at which such license agreement becomes effective.
#
#  In case a separate license agreement ends, and such agreement ends without being
#  replaced by another separate license agreement, the license terms below apply
#  from the time at which said agreement ends.
#
#  LICENSE TERMS
#
#  This program is free software: you can redistribute it and/or modify it under the
#  terms of the GNU Affero General Public License, version 3, as published by the
#  Free Software Foundation. This program is distributed in the hope that it will be
#  useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
#  See the GNU Affero General Public License Version 3 for more details.
#
#  You should have received a copy of the GNU Affero General Public license along
#  with this program. If not, see <http://www.gnu.org/licenses/agpl-3.0.en.html>.
#
#####################################################################################

from __future__ import absolute_import, division, print_function

import json

from txaio import make_logger

from zope.interface import implementer

from collections import OrderedDict

from twisted.internet.interfaces import IHandshakeListener, ISSLTransport
from twisted.internet.protocol import Protocol, Factory
from twisted.internet.defer import succeed, inlineCallbacks, Deferred

from crossbar.adapter.mqtt.tx import MQTTServerTwistedProtocol
from crossbar.router.session import RouterSession

from autobahn import util
from autobahn.wamp import message, role
from autobahn.wamp.types import PublishOptions
from autobahn.twisted.util import transport_channel_id


def tokenise_mqtt_topic(topic):
    """
    Limitedly WAMP-ify and break it down into WAMP-like tokens.
    """
    assert len(topic) > 0
    topic = topic.replace(u"+", u"*")

    return topic.split(u"/")


def tokenise_wamp_topic(topic):
    """
    Limitedly MQTT-ify and break it down into MQTT-like tokens.
    """
    assert len(topic) > 0
    topic = topic.replace(u"*", u"+")

    return topic.split(u".")


class WampTransport(object):
    _authid = None

    def __init__(self, on_message, real_transport):
        self.on_message = on_message
        self.transport = real_transport

    def send(self, msg):
        self.on_message(msg)

    def get_channel_id(self, channel_id_type=u'tls-unique'):
        return transport_channel_id(self.transport, is_server=True, channel_id_type=channel_id_type)


@implementer(IHandshakeListener)
class WampMQTTServerProtocol(Protocol):

    log = make_logger()

    def __init__(self, reactor):
        self._mqtt = MQTTServerTwistedProtocol(self, reactor)
        self._request_to_packetid = {}
        self._waiting_for_connect = Deferred()
        self._inflight_subscriptions = {}
        self._subrequest_to_mqtt_subrequest = {}
        self._subrequest_callbacks = {}
        self._topic_lookup = {}

    def on_message(self, inc_msg):

        try:
            self._on_message(inc_msg)
        except:
            self.log.failure()

    def _on_message(self, inc_msg):

        if isinstance(inc_msg, message.Challenge):
            assert inc_msg.method == u"ticket"

            msg = message.Authenticate(signature=self._pw_challenge)
            del self._pw_challenge

            self._wamp_session.onMessage(msg)

        elif isinstance(inc_msg, message.Welcome):
            self._waiting_for_connect.callback((0, False))

        elif isinstance(inc_msg, message.Abort):
            self._waiting_for_connect.callback((1, False))

        elif isinstance(inc_msg, message.Subscribed):
            # Successful subscription!
            mqtt_id = self._subrequest_to_mqtt_subrequest[inc_msg.request]
            self._inflight_subscriptions[mqtt_id][inc_msg.request]["response"] = 0
            self._topic_lookup[inc_msg.subscription] = self._inflight_subscriptions[mqtt_id][inc_msg.request]["topic"]

            if -1 not in [x["response"] for x in self._inflight_subscriptions[mqtt_id].values()]:
                self._subrequest_callbacks[mqtt_id].callback(None)

        elif (isinstance(inc_msg, message.Error) and
              inc_msg.request_type == message.Subscribe.MESSAGE_TYPE):
            # Failed subscription :(
            mqtt_id = self._subrequest_to_mqtt_subrequest[inc_msg.request]
            self._inflight_subscriptions[mqtt_id][inc_msg.request]["response"] = 128

            if -1 not in [x["response"] for x in self._inflight_subscriptions[mqtt_id].values()]:
                self._subrequest_callbacks[mqtt_id].callback(None)

        elif isinstance(inc_msg, message.Event):

            topic = inc_msg.topic or self._topic_lookup[inc_msg.subscription]

            # Should be real encoding...
            body = json.dumps({"args": inc_msg.args or [],
                               "kwargs": inc_msg.kwargs or {}},
                              sort_keys=True, ensure_ascii=False).encode('utf8')

            self._mqtt.send_publish(u"/".join(tokenise_wamp_topic(topic)), 0, body)

    def connectionMade(self):
        if not ISSLTransport.providedBy(self.transport):
            self._when_ready()

    def handshakeCompleted(self):
        self._when_ready()

    def _when_ready(self):
        self._mqtt.transport = self.transport

        self._wamp_session = RouterSession(self.factory._wamp_session_factory._routerFactory)
        self._wamp_transport = WampTransport(self.on_message, self.transport)
        self._wamp_transport.factory = self.factory
        self._wamp_session.onOpen(self._wamp_transport)

    def process_connect(self, packet):

        roles = {
            u"subscriber": role.RoleSubscriberFeatures(
                payload_transparency=True),
            u"publisher": role.RolePublisherFeatures(
                payload_transparency=True,
                x_acknowledged_event_delivery=True)
        }

        # Will be autoassigned
        realm = None
        methods = []

        if ISSLTransport.providedBy(self.transport):
            methods.append(u"tls")

        if packet.username and packet.password:
            methods.append(u"ticket")
            msg = message.Hello(
                realm=realm,
                roles=roles,
                authmethods=methods,
                authid=packet.username)
            self._pw_challenge = packet.password

        else:
            methods.append(u"anonymous")
            msg = message.Hello(
                realm=realm,
                roles=roles,
                authmethods=methods,
                authid=packet.client_id)

        self._wamp_session.onMessage(msg)

        # Should add some authorisation here?
        return self._waiting_for_connect

    def _publish(self, event, options):

        request = util.id()
        msg = message.Publish(
            request=request,
            topic=event.topic_name,
            args=tuple(),
            kwargs={'mqtt_message': event.payload.decode('utf8'),
                    'mqtt_qos': event.qos_level},
            **options.message_attr())

        self._wamp_session.onMessage(msg)

        if event.qos_level > 0:
            self._request_to_packetid[request] = event.packet_identifier

        return succeed(0)

    def process_publish_qos_0(self, event):
        return self._publish(event, options=PublishOptions(exclude_me=False))

    def process_publish_qos_1(self, event):
        return self._publish(event,
                             options=PublishOptions(acknowledge=True, exclude_me=False))

    def process_puback(self, event):
        return

    def process_pubrec(self, event):
        return

    def process_pubrel(self, event):
        return

    def process_pubcomp(self, event):
        return

    def process_subscribe(self, packet):

        packet_watch = OrderedDict()
        d = Deferred()

        @d.addCallback
        def _(ign):
            self._mqtt.send_suback(packet.packet_identifier, [x["response"] for x in packet_watch.values()])
            del self._inflight_subscriptions[packet.packet_identifier]
            del self._subrequest_callbacks[packet.packet_identifier]

        self._subrequest_callbacks[packet.packet_identifier] = d
        self._inflight_subscriptions[packet.packet_identifier] = packet_watch

        for n, x in enumerate(packet.topic_requests):
            # fixme
            match_type = u"exact"

            request_id = util.id()

            msg = message.Subscribe(
                request=request_id,
                topic=u".".join(tokenise_mqtt_topic(x.topic_filter)),
                match=match_type)

            try:
                packet_watch[request_id] = {"response": -1, "topic": x.topic_filter}
                self._subrequest_to_mqtt_subrequest[request_id] = packet.packet_identifier
                self._wamp_session.onMessage(msg)
            except Exception:
                self.log.failure()
                packet_watch[request_id] = {"response": 128}

    @inlineCallbacks
    def process_unsubscribe(self, packet):

        for topic in packet.topics:
            if topic in self._subscriptions:
                yield self._subscriptions.pop(topic).unsubscribe()

        return

    def dataReceived(self, data):
        self._mqtt.dataReceived(data)


class WampMQTTServerFactory(Factory):

    protocol = WampMQTTServerProtocol

    def __init__(self, session_factory, config, reactor):
        self._wamp_session_factory = session_factory
        self._config = config["options"]
        self._reactor = reactor

    def buildProtocol(self, addr):

        protocol = self.protocol(self._reactor)
        protocol.factory = self
        return protocol
