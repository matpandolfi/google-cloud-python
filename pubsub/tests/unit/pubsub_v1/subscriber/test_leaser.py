# Copyright 2017, Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import threading

from google.cloud.pubsub_v1 import types
from google.cloud.pubsub_v1.subscriber import subscriber
from google.cloud.pubsub_v1.subscriber._protocol import histogram
from google.cloud.pubsub_v1.subscriber._protocol import leaser
from google.cloud.pubsub_v1.subscriber._protocol import requests

import mock
import pytest


def test_add_and_remove():
    leaser_ = leaser.Leaser(mock.sentinel.subscriber)

    leaser_.add([
        requests.LeaseRequest(ack_id='ack1', byte_size=50)])
    leaser_.add([
        requests.LeaseRequest(ack_id='ack2', byte_size=25)])

    assert leaser_.message_count == 2
    assert set(leaser_.ack_ids) == set(['ack1', 'ack2'])
    assert leaser_.bytes == 75

    leaser_.remove([
        requests.DropRequest(ack_id='ack1', byte_size=50)])

    assert leaser_.message_count == 1
    assert set(leaser_.ack_ids) == set(['ack2'])
    assert leaser_.bytes == 25


def test_add_already_managed(caplog):
    caplog.set_level(logging.DEBUG)

    leaser_ = leaser.Leaser(mock.sentinel.subscriber)

    leaser_.add([
        requests.LeaseRequest(ack_id='ack1', byte_size=50)])
    leaser_.add([
        requests.LeaseRequest(ack_id='ack1', byte_size=50)])

    assert 'already lease managed' in caplog.text


def test_remove_not_managed(caplog):
    caplog.set_level(logging.DEBUG)

    leaser_ = leaser.Leaser(mock.sentinel.subscriber)

    leaser_.remove([
        requests.DropRequest(ack_id='ack1', byte_size=50)])

    assert 'not managed' in caplog.text


def test_remove_negative_bytes(caplog):
    caplog.set_level(logging.DEBUG)

    leaser_ = leaser.Leaser(mock.sentinel.subscriber)

    leaser_.add([
        requests.LeaseRequest(ack_id='ack1', byte_size=50)])
    leaser_.remove([
        requests.DropRequest(ack_id='ack1', byte_size=75)])

    assert leaser_.bytes == 0
    assert 'unexpectedly negative' in caplog.text


def create_subscriber(flow_control=types.FlowControl()):
    subscriber_ = mock.create_autospec(subscriber.Subscriber, instance=True)
    subscriber_.is_active = True
    subscriber_.flow_control = flow_control
    subscriber_.ack_histogram = histogram.Histogram()
    return subscriber_


def test_maintain_leases_inactive(caplog):
    caplog.set_level(logging.INFO)
    subscriber_ = create_subscriber()
    subscriber_.is_active = False

    leaser_ = leaser.Leaser(subscriber_)

    leaser_.maintain_leases()

    assert 'exiting' in caplog.text


def test_maintain_leases_stopped(caplog):
    caplog.set_level(logging.INFO)
    subscriber_ = create_subscriber()

    leaser_ = leaser.Leaser(subscriber_)
    leaser_.stop()

    leaser_.maintain_leases()

    assert 'exiting' in caplog.text


def make_sleep_mark_subscriber_as_inactive(sleep, subscriber):
    # Make sleep mark the subscriber as inactive so that maintain_leases
    # exits at the end of the first run.
    def trigger_inactive(seconds):
        assert 0 < seconds < 10
        subscriber.is_active = False
    sleep.side_effect = trigger_inactive


@mock.patch('time.sleep', autospec=True)
def test_maintain_leases_ack_ids(sleep):
    subscriber_ = create_subscriber()
    make_sleep_mark_subscriber_as_inactive(sleep, subscriber_)
    leaser_ = leaser.Leaser(subscriber_)
    leaser_.add([requests.LeaseRequest(ack_id='my ack id', byte_size=50)])

    leaser_.maintain_leases()

    subscriber_.modify_ack_deadline.assert_called_once_with([
        requests.ModAckRequest(
            ack_id='my ack id',
            seconds=10,
        )
    ])
    sleep.assert_called()


@mock.patch('time.sleep', autospec=True)
def test_maintain_leases_no_ack_ids(sleep):
    subscriber_ = create_subscriber()
    make_sleep_mark_subscriber_as_inactive(sleep, subscriber_)
    leaser_ = leaser.Leaser(subscriber_)

    leaser_.maintain_leases()

    subscriber_.modify_ack_deadline.assert_not_called()
    sleep.assert_called()


@mock.patch('time.time', autospec=True)
@mock.patch('time.sleep', autospec=True)
def test_maintain_leases_outdated_items(sleep, time):
    subscriber_ = create_subscriber()
    make_sleep_mark_subscriber_as_inactive(sleep, subscriber_)
    leaser_ = leaser.Leaser(subscriber_)

    # Add these items at the beginning of the timeline
    time.return_value = 0
    leaser_.add([
        requests.LeaseRequest(ack_id='ack1', byte_size=50)])

    # Add another item at towards end of the timeline
    time.return_value = subscriber_.flow_control.max_lease_duration - 1
    leaser_.add([
        requests.LeaseRequest(ack_id='ack2', byte_size=50)])

    # Now make sure time reports that we are at the end of our timeline.
    time.return_value = subscriber_.flow_control.max_lease_duration + 1

    leaser_.maintain_leases()

    # Only ack2 should be renewed. ack1 should've been dropped
    subscriber_.modify_ack_deadline.assert_called_once_with([
        requests.ModAckRequest(
            ack_id='ack2',
            seconds=10,
        )
    ])
    subscriber_.drop.assert_called_once_with([
        requests.DropRequest(ack_id='ack1', byte_size=50)
    ])
    sleep.assert_called()


@mock.patch('threading.Thread', autospec=True)
def test_start(thread):
    subscriber_ = mock.create_autospec(subscriber.Subscriber, instance=True)
    leaser_ = leaser.Leaser(subscriber_)

    leaser_.start()

    thread.assert_called_once_with(
        name=leaser._LEASE_WORKER_NAME, target=leaser_.maintain_leases)

    thread.return_value.start.assert_called_once()

    assert leaser_._thread is not None


@mock.patch('threading.Thread', autospec=True)
def test_start_already_started(thread):
    subscriber_ = mock.create_autospec(subscriber.Subscriber, instance=True)
    leaser_ = leaser.Leaser(subscriber_)
    leaser_._thread = mock.sentinel.thread

    with pytest.raises(ValueError):
        leaser_.start()

    thread.assert_not_called()


def test_stop():
    subscriber_ = mock.create_autospec(subscriber.Subscriber, instance=True)
    leaser_ = leaser.Leaser(subscriber_)
    thread = mock.create_autospec(threading.Thread, instance=True)
    leaser_._thread = thread

    leaser_.stop()

    assert leaser_._stop_event.is_set()
    thread.join.assert_called_once()
    assert leaser_._thread is None


def test_stop_no_join():
    leaser_ = leaser.Leaser(mock.sentinel.subscriber)

    leaser_.stop()
