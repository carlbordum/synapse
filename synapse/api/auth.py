# -*- coding: utf-8 -*-
# Copyright 2014 OpenMarket Ltd
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

"""This module contains classes for authenticating the user."""

from twisted.internet import defer

from synapse.api.constants import Membership, JoinRules
from synapse.api.errors import AuthError, StoreError, Codes, SynapseError
from synapse.api.events.room import (
    RoomMemberEvent, RoomPowerLevelsEvent, RoomRedactionEvent,
    RoomJoinRulesEvent, RoomCreateEvent,
)
from synapse.util.logutils import log_function

import logging

logger = logging.getLogger(__name__)


class Auth(object):

    def __init__(self, hs):
        self.hs = hs
        self.store = hs.get_datastore()

    def check(self, event, raises=False):
        """ Checks if this event is correctly authed.

        Returns:
            True if the auth checks pass.
        Raises:
            AuthError if there was a problem authorising this event. This will
            be raised only if raises=True.
        """
        try:
            if hasattr(event, "room_id"):
                if event.old_state_events is None:
                    # Oh, we don't know what the state of the room was, so we
                    # are trusting that this is allowed (at least for now)
                    logger.warn("Trusting event: %s", event.event_id)
                    return True

                if hasattr(event, "outlier") and event.outlier is True:
                    # TODO (erikj): Auth for outliers is done differently.
                    return True

                if event.type == RoomCreateEvent.TYPE:
                    # FIXME
                    return True

                self._can_send_event(event)

                if event.type == RoomMemberEvent.TYPE:
                    allowed = self.is_membership_change_allowed(event)
                    if allowed:
                        logger.debug("Allowing! %s", event)
                    else:
                        logger.debug("Denying! %s", event)
                    return allowed

                if event.type == RoomPowerLevelsEvent.TYPE:
                    self._check_power_levels(event)

                if event.type == RoomRedactionEvent.TYPE:
                    self._check_redaction(event)

                logger.debug("Allowing! %s", event)
                return True
            else:
                raise AuthError(500, "Unknown event: %s" % event)
        except AuthError as e:
            logger.info("Event auth check failed on event %s with msg: %s",
                        event, e.msg)
            logger.info("Denying! %s", event)
            if raises:
                raise e

        return False

    @defer.inlineCallbacks
    def check_joined_room(self, room_id, user_id):
        try:
            member = yield self.store.get_room_member(
                room_id=room_id,
                user_id=user_id
            )
            self._check_joined_room(member, user_id, room_id)
            defer.returnValue(member)
        except AttributeError:
            pass
        defer.returnValue(None)

    def check_event_sender_in_room(self, event):
        key = (RoomMemberEvent.TYPE, event.user_id, )
        member_event = event.state_events.get(key)

        return self._check_joined_room(
            member_event,
            event.user_id,
            event.room_id
        )

    def _check_joined_room(self, member, user_id, room_id):
        if not member or member.membership != Membership.JOIN:
            raise AuthError(403, "User %s not in room %s (%s)" % (
                user_id, room_id, repr(member)
            ))

    @log_function
    def is_membership_change_allowed(self, event):
        target_user_id = event.state_key

        # get info about the caller
        key = (RoomMemberEvent.TYPE, event.user_id, )
        caller = event.old_state_events.get(key)

        caller_in_room = caller and caller.membership == "join"

        # get info about the target
        key = (RoomMemberEvent.TYPE, target_user_id, )
        target = event.old_state_events.get(key)

        target_in_room = target and target.membership == "join"

        membership = event.content["membership"]

        key = (RoomJoinRulesEvent.TYPE, "", )
        join_rule_event = event.old_state_events.get(key)
        if join_rule_event:
            join_rule = join_rule_event.content.get(
                "join_rule", JoinRules.INVITE
            )
        else:
            join_rule = JoinRules.INVITE

        user_level = self._get_power_level_from_event_state(
            event,
            event.user_id,
        )

        ban_level, kick_level, redact_level = (
            self._get_ops_level_from_event_state(
                event
            )
        )

        logger.debug(
            "is_membership_change_allowed: %s",
            {
                "caller_in_room": caller_in_room,
                "target_in_room": target_in_room,
                "membership": membership,
                "join_rule": join_rule,
                "target_user_id": target_user_id,
                "event.user_id": event.user_id,
            }
        )

        if Membership.INVITE == membership:
            # TODO (erikj): We should probably handle this more intelligently
            # PRIVATE join rules.

            # Invites are valid iff caller is in the room and target isn't.
            if not caller_in_room:  # caller isn't joined
                raise AuthError(403, "You are not in room %s." % event.room_id)
            elif target_in_room:  # the target is already in the room.
                raise AuthError(403, "%s is already in the room." %
                                     target_user_id)
        elif Membership.JOIN == membership:
            # Joins are valid iff caller == target and they were:
            # invited: They are accepting the invitation
            # joined: It's a NOOP
            if event.user_id != target_user_id:
                raise AuthError(403, "Cannot force another user to join.")
            elif join_rule == JoinRules.PUBLIC:
                pass
            elif join_rule == JoinRules.INVITE:
                if not caller_in_room:
                    raise AuthError(403, "You are not invited to this room.")
            else:
                # TODO (erikj): may_join list
                # TODO (erikj): private rooms
                raise AuthError(403, "You are not allowed to join this room")
        elif Membership.LEAVE == membership:
            # TODO (erikj): Implement kicks.

            if not caller_in_room:  # trying to leave a room you aren't joined
                raise AuthError(403, "You are not in room %s." % event.room_id)
            elif target_user_id != event.user_id:
                if kick_level:
                    kick_level = int(kick_level)
                else:
                    kick_level = 50  # FIXME (erikj): What should we do here?

                if user_level < kick_level:
                    raise AuthError(
                        403, "You cannot kick user %s." % target_user_id
                    )
        elif Membership.BAN == membership:
            if ban_level:
                ban_level = int(ban_level)
            else:
                ban_level = 50  # FIXME (erikj): What should we do here?

            if user_level < ban_level:
                raise AuthError(403, "You don't have permission to ban")
        else:
            raise AuthError(500, "Unknown membership %s" % membership)

        return True

    def _get_power_level_from_event_state(self, event, user_id):
        key = (RoomPowerLevelsEvent.TYPE, "", )
        power_level_event = event.old_state_events.get(key)
        level = None
        if power_level_event:
            level = power_level_event.content.get("users", {}).get(user_id)
            if not level:
                level = power_level_event.content.get("users_default", 0)

        return level

    def _get_ops_level_from_event_state(self, event):
        key = (RoomPowerLevelsEvent.TYPE, "", )
        power_level_event = event.old_state_events.get(key)

        if power_level_event:
            return (
                power_level_event.content.get("ban", 50),
                power_level_event.content.get("kick", 50),
                power_level_event.content.get("redact", 50),
            )
        return None, None, None,

    @defer.inlineCallbacks
    def get_user_by_req(self, request):
        """ Get a registered user's ID.

        Args:
            request - An HTTP request with an access_token query parameter.
        Returns:
            UserID : User ID object of the user making the request
        Raises:
            AuthError if no user by that token exists or the token is invalid.
        """
        # Can optionally look elsewhere in the request (e.g. headers)
        try:
            access_token = request.args["access_token"][0]
            user_info = yield self.get_user_by_token(access_token)
            user = user_info["user"]

            ip_addr = self.hs.get_ip_from_request(request)
            user_agent = request.requestHeaders.getRawHeaders(
                "User-Agent",
                default=[""]
            )[0]
            if user and access_token and ip_addr:
                self.store.insert_client_ip(
                    user=user,
                    access_token=access_token,
                    device_id=user_info["device_id"],
                    ip=ip_addr,
                    user_agent=user_agent
                )

            defer.returnValue(user)
        except KeyError:
            raise AuthError(403, "Missing access token.")

    @defer.inlineCallbacks
    def get_user_by_token(self, token):
        """ Get a registered user's ID.

        Args:
            token (str): The access token to get the user by.
        Returns:
            dict : dict that includes the user, device_id, and whether the
                user is a server admin.
        Raises:
            AuthError if no user by that token exists or the token is invalid.
        """
        try:
            ret = yield self.store.get_user_by_token(token=token)
            if not ret:
                raise StoreError()

            user_info = {
                "admin": bool(ret.get("admin", False)),
                "device_id": ret.get("device_id"),
                "user": self.hs.parse_userid(ret.get("name")),
            }

            defer.returnValue(user_info)
        except StoreError:
            raise AuthError(403, "Unrecognised access token.",
                            errcode=Codes.UNKNOWN_TOKEN)

    def is_server_admin(self, user):
        return self.store.is_server_admin(user)

    @log_function
    def _can_send_event(self, event):
        key = (RoomPowerLevelsEvent.TYPE, "", )
        send_level_event = event.old_state_events.get(key)
        send_level = None
        if send_level_event:
            send_level = send_level_event.content.get("events", {}).get(
                event.type
            )
            if not send_level:
                if hasattr(event, "state_key"):
                    send_level = send_level_event.content.get(
                        "state_default", 50
                    )
                else:
                    send_level = send_level_event.content.get(
                        "events_default", 0
                    )

        if send_level:
            send_level = int(send_level)
        else:
            send_level = 0

        user_level = self._get_power_level_from_event_state(
            event,
            event.user_id,
        )

        if user_level:
            user_level = int(user_level)
        else:
            user_level = 0

        if user_level < send_level:
            raise AuthError(
                403, "You don't have permission to post that to the room"
            )

        return True

    def _check_redaction(self, event):
        user_level = self._get_power_level_from_event_state(
            event,
            event.user_id,
        )

        _, _, redact_level = self._get_ops_level_from_event_state(
            event
        )

        if user_level < redact_level:
            raise AuthError(
                403,
                "You don't have permission to redact events"
            )

    def _check_power_levels(self, event):
        user_list = event.content.get("users", {})
        # Validate users
        for k, v in user_list.items():
            try:
                self.hs.parse_userid(k)
            except:
                raise SynapseError(400, "Not a valid user_id: %s" % (k,))

            try:
                int(v)
            except:
                raise SynapseError(400, "Not a valid power level: %s" % (v,))

        key = (event.type, event.state_key, )
        current_state = event.old_state_events.get(key)

        if not current_state:
            return

        user_level = self._get_power_level_from_event_state(
            event,
            event.user_id,
        )

       # Check other levels:
        levels_to_check = [
            ("users_default", []),
            ("events_default", []),
            ("ban", []),
            ("redact", []),
            ("kick", []),
        ]

        old_list = current_state.content.get("users")
        for user in set(old_list.keys() + user_list.keys()):
            levels_to_check.append(
                (user, ["users"])
            )

        old_list = current_state.content.get("events")
        new_list = event.content.get("events")
        for ev_id in set(old_list.keys() + new_list.keys()):
            levels_to_check.append(
                (ev_id, ["events"])
            )

        old_state = current_state.content
        new_state = event.content

        for level_to_check, dir in levels_to_check:
            old_loc = old_state
            for d in dir:
                old_loc = old_loc.get(d, {})

            new_loc = new_state
            for d in dir:
                new_loc = new_loc.get(d, {})

            if level_to_check in old_loc:
                old_level = int(old_loc[level_to_check])
            else:
                old_level = None

            if level_to_check in new_loc:
                new_level = int(new_loc[level_to_check])
            else:
                new_level = None

            if new_level is not None and old_level is not None:
                if new_level == old_level:
                    continue

            if old_level > user_level or new_level > user_level:
                raise AuthError(
                    403,
                    "You don't have permission to add ops level greater "
                    "than your own"
                )
