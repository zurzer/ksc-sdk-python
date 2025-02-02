# Copyright 2016 ksyun.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
import copy
from collections import deque
from kscore.validate import validate_parameters
from kscore.exceptions import ParamValidationError, \
    StubResponseError, StubAssertionError
from requests.models import Response


class Stubber(object):
    """
    This class will allow you to stub out requests so you don't have to hit
    an endpoint to write tests. Responses are returned first in, first out.
    If operations are called out of order, or are called with no remaining
    queued responses, an error will be raised.

    **Example:**
    ::
        import datetime
        import kscore.session
        from kscore.stub import Stubber


        s3 = kscore.session.get_session().create_client('s3')
        stubber = Stubber(s3)

        response = {
            'IsTruncated': False,
            'Name': 'test-bucket',
            'MaxKeys': 1000, 'Prefix': '',
            'Contents': [{
                'Key': 'test.txt',
                'ETag': '"abc123"',
                'StorageClass': 'STANDARD',
                'LastModified': datetime.datetime(2016, 1, 20, 22, 9),
                'Owner': {'ID': 'abc123', 'DisplayName': 'myname'},
                'Size': 14814
            }],
            'EncodingType': 'url',
            'ResponseMetadata': {
                'RequestId': 'abc123',
                'HTTPStatusCode': 200,
                'HostId': 'abc123'
            },
            'Marker': ''
        }

        expected_params = {'Bucket': 'test-bucket'}

        stubber.add_response('list_objects', response, expected_params)
        stubber.activate()

        service_response = s3.list_objects(Bucket='test-bucket')
        assert service_response == response
    """
    def __init__(self, client):
        """
        :param client: The client to add your stubs to.
        """
        self.client = client
        self._event_id = 'ksc_stubber'
        self._expected_params_event_id = 'ksc_stubber_expected_params'
        self._queue = deque()

    def activate(self):
        """
        Activates the stubber on the client
        """
        self.client.meta.events.register_first(
            'before-parameter-build.*.*',
            self._assert_expected_params,
            unique_id=self._expected_params_event_id)
        self.client.meta.events.register(
            'before-call.*.*',
            self._get_response_handler,
            unique_id=self._event_id)

    def deactivate(self):
        """
        Deactivates the stubber on the client
        """
        self.client.meta.events.unregister(
            'before-parameter-build.*.*',
            self._assert_expected_params,
            unique_id=self._expected_params_event_id)
        self.client.meta.events.unregister(
            'before-call.*.*',
            self._get_response_handler,
            unique_id=self._event_id)

    def add_response(self, method, service_response, expected_params=None):
        """
        Adds a service response to the response queue. This will be validated
        against the service model to ensure correctness. It should be noted,
        however, that while missing attributes are often considered correct,
        your code may not function properly if you leave them out. Therefore
        you should always fill in every value you see in a typical response for
        your particular request.

        :param method: The name of the client method to stub.
        :type method: str

        :param service_response: A dict response stub. Provided parameters will
            be validated against the service model.
        :type service_response: dict

        :param expected_params: A dictionary of the expected parameters to
            be called for the provided service response. The parameters match
            the names of keyword arguments passed to that client call. If
            any of the parameters differ a ``StubResponseError`` is thrown.
        """
        self._add_response(method, service_response, expected_params)

    def _add_response(self, method, service_response, expected_params):
        if not hasattr(self.client, method):
            raise ValueError(
                "Client %s does not have method: %s"
                % (self.client.meta.service_model.service_name, method))

        # Create a successful http response
        http_response = Response()
        http_response.status_code = 200
        http_response.reason = 'OK'

        operation_name = self.client.meta.method_to_api_mapping.get(method)
        self._validate_response(operation_name, service_response)

        # Add the service_response to the queue for returning responses
        response = {
            'operation_name': operation_name,
            'response': (http_response, service_response),
            'expected_params': expected_params
        }
        self._queue.append(response)

    def add_client_error(self, method, service_error_code='',
                         service_message='', http_status_code=400):
        """
        Adds a ``ClientError`` to the response queue.

        :param method: The name of the service method to return the error on.
        :type method: str

        :param service_error_code: The service error code to return,
                                   e.g. ``NoSuchBucket``
        :type service_error_code: str

        :param service_message: The service message to return, e.g.
                        'The specified bucket does not exist.'
        :type service_message: str

        :param http_status_code: The HTTP status code to return, e.g. 404, etc
        :type http_status_code: int
        """
        http_response = Response()
        http_response.status_code = http_status_code

        # We don't look to the model to build this because the caller would
        # need to know the details of what the HTTP body would need to
        # look like.
        parsed_response = {
            'ResponseMetadata': {'HTTPStatusCode': http_status_code},
            'Error': {
                'Message': service_message,
                'Code': service_error_code
            }
        }

        operation_name = self.client.meta.method_to_api_mapping.get(method)
        # Note that we do not allow for expected_params while
        # adding errors into the queue yet.
        response = {
            'operation_name': operation_name,
            'response': (http_response, parsed_response),
            'expected_params': None
        }
        self._queue.append(response)

    def assert_no_pending_responses(self):
        """
        Asserts that all expected calls were made.
        """
        remaining = len(self._queue)
        if remaining != 0:
            raise AssertionError(
                "%d responses remaining in queue." % remaining)

    def _assert_expected_call_order(self, model, params):
        if not self._queue:
            raise StubResponseError(
                operation_name=model.name,
                reason='Unexpected API Call: called with parameters %s' %
                       params)

        name = self._queue[0]['operation_name']
        if name != model.name:
            raise StubResponseError(
                operation_name=model.name,
                reason='Operation mismatch: found response for %s.' % name)

    def _get_response_handler(self, model, params, **kwargs):
        self._assert_expected_call_order(model, params)
        # Pop off the entire response once everything has been validated
        return self._queue.popleft()['response']

    def _assert_expected_params(self, model, params, **kwargs):
        self._assert_expected_call_order(model, params)
        expected_params = self._queue[0]['expected_params']
        if expected_params is not None and params != expected_params:
            raise StubAssertionError(
                operation_name=model.name,
                reason='Expected parameters: %s, but received: %s' % (
                    expected_params, params))

    def _validate_response(self, operation_name, service_response):
        service_model = self.client.meta.service_model
        operation_model = service_model.operation_model(operation_name)
        output_shape = operation_model.output_shape

        # Remove ResponseMetadata so that the validator doesn't attempt to
        # perform validation on it.
        response = service_response
        if 'ResponseMetadata' in response:
            response = copy.copy(service_response)
            del response['ResponseMetadata']

        if output_shape is not None:
            validate_parameters(response, output_shape)
        elif response:
            # If the output shape is None, that means the response should be
            # empty apart from ResponseMetadata
            raise ParamValidationError(
                report=(
                    "Service response should only contain ResponseMetadata."))
