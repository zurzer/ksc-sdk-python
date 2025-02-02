# -*- coding: utf-8 -*-
# Copyright (c) 2012-2013 LiuYC https://github.com/liuyichen/
# Copyright 2012-2014 ksyun.com, Inc. or its affiliates. All Rights Reserved.
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

from __future__ import unicode_literals
from requests.exceptions import ConnectionError

import sys
if sys.version_info[0] < 3 :
    from imp import reload
    reload(sys)
    sys.setdefaultencoding('utf8')


class KSCoreError(Exception):
    """
    The base exception class for KSCore exceptions.

    :ivar msg: The descriptive message associated with the error.
    """
    fmt = 'An unspecified error occurred'

    def __init__(self, **kwargs):
        msg = self.fmt.format(**kwargs)
        Exception.__init__(self, msg)
        self.kwargs = kwargs


class DataNotFoundError(KSCoreError):
    """
    The data associated with a particular path could not be loaded.

    :ivar path: The data path that the user attempted to load.
    """
    fmt = 'Unable to load data for: {data_path}'


class UnknownServiceError(DataNotFoundError):
    """Raised when trying to load data for an unknown service.

    :ivar service_name: The name of the unknown service.

    """
    fmt = (
        "Unknown service: '{service_name}'. Valid service names are: "
        "{known_service_names}")


class ApiVersionNotFoundError(KSCoreError):
    """
    The data associated with either that API version or a compatible one
    could not be loaded.

    :ivar path: The data path that the user attempted to load.
    :ivar path: The API version that the user attempted to load.
    """
    fmt = 'Unable to load data {data_path} for: {api_version}'


class EndpointConnectionError(KSCoreError):
    fmt = (
        'Could not connect to the endpoint URL: "{endpoint_url}"')


class ConnectionClosedError(ConnectionError):
    fmt = (
        'Connection was closed before we received a valid response '
        'from endpoint URL: "{endpoint_url}".')
    def __init__(self, **kwargs):
        msg = self.fmt.format(**kwargs)
        kwargs.pop('endpoint_url')
        super(ConnectionClosedError, self).__init__(msg, **kwargs)


class NoCredentialsError(KSCoreError):
    """
    No credentials could be found
    """
    fmt = 'Unable to locate credentials'


class PartialCredentialsError(KSCoreError):
    """
    Only partial credentials were found.

    :ivar cred_var: The missing credential variable name.

    """
    fmt = 'Partial credentials found in {provider}, missing: {cred_var}'


class UnknownSignatureVersionError(KSCoreError):
    """
    Requested Signature Version is not known.

    :ivar signature_version: The name of the requested signature version.
    """
    fmt = 'Unknown Signature Version: {signature_version}.'


class ServiceNotInRegionError(KSCoreError):
    """
    The service is not available in requested region.

    :ivar service_name: The name of the service.
    :ivar region_name: The name of the region.
    """
    fmt = 'Service {service_name} not available in region {region_name}'


class BaseEndpointResolverError(KSCoreError):
    """Base error for endpoint resolving errors.

    Should never be raised directly, but clients can catch
    this exception if they want to generically handle any errors
    during the endpoint resolution process.

    """


class NoRegionError(BaseEndpointResolverError):
    """No region was specified."""
    fmt = 'You must specify a region.'


class UnknownEndpointError(BaseEndpointResolverError, ValueError):
    """
    Could not construct an endpoint.

    :ivar service_name: The name of the service.
    :ivar region_name: The name of the region.
    """
    fmt = (
        'Unable to construct an endpoint for '
        '{service_name} in region {region_name}')


class ProfileNotFound(KSCoreError):
    """
    The specified configuration profile was not found in the
    configuration file.

    :ivar profile: The name of the profile the user attempted to load.
    """
    fmt = 'The config profile ({profile}) could not be found'


class ConfigParseError(KSCoreError):
    """
    The configuration file could not be parsed.

    :ivar path: The path to the configuration file.
    """
    fmt = 'Unable to parse config file: {path}'


class ConfigNotFound(KSCoreError):
    """
    The specified configuration file could not be found.

    :ivar path: The path to the configuration file.
    """
    fmt = 'The specified config file ({path}) could not be found.'


class MissingParametersError(KSCoreError):
    """
    One or more required parameters were not supplied.

    :ivar object: The object that has missing parameters.
        This can be an operation or a parameter (in the
        case of inner params).  The str() of this object
        will be used so it doesn't need to implement anything
        other than str().
    :ivar missing: The names of the missing parameters.
    """
    fmt = ('The following required parameters are missing for '
           '{object_name}: {missing}')


class ValidationError(KSCoreError):
    """
    An exception occurred validating parameters.

    Subclasses must accept a ``value`` and ``param``
    argument in their ``__init__``.

    :ivar value: The value that was being validated.
    :ivar param: The parameter that failed validation.
    :ivar type_name: The name of the underlying type.
    """
    fmt = ("Invalid value ('{value}') for param {param} "
           "of type {type_name} ")


class ParamValidationError(KSCoreError):
    fmt = 'Parameter validation failed:\n{report}'


# These exceptions subclass from ValidationError so that code
# can just 'except ValidationError' to catch any possibly validation
# error.
class UnknownKeyError(ValidationError):
    """
    Unknown key in a struct paramster.

    :ivar value: The value that was being checked.
    :ivar param: The name of the parameter.
    :ivar choices: The valid choices the value can be.
    """
    fmt = ("Unknown key '{value}' for param '{param}'.  Must be one "
           "of: {choices}")


class RangeError(ValidationError):
    """
    A parameter value was out of the valid range.

    :ivar value: The value that was being checked.
    :ivar param: The parameter that failed validation.
    :ivar min_value: The specified minimum value.
    :ivar max_value: The specified maximum value.
    """
    fmt = ('Value out of range for param {param}: '
           '{min_value} <= {value} <= {max_value}')


class UnknownParameterError(ValidationError):
    """
    Unknown top level parameter.

    :ivar name: The name of the unknown parameter.
    :ivar operation: The name of the operation.
    :ivar choices: The valid choices the parameter name can be.
    """
    fmt = (
        "Unknown parameter '{name}' for operation {operation}.  Must be one "
        "of: {choices}"
    )


class AliasConflictParameterError(ValidationError):
    """
    Error when an alias is provided for a parameter as well as the original.

    :ivar original: The name of the original parameter.
    :ivar alias: The name of the alias
    :ivar operation: The name of the operation.
    """
    fmt = (
        "Parameter '{original}' and its alias '{alias}' were provided "
        "for operation {operation}.  Only one of them may be used."
    )


class UnknownServiceStyle(KSCoreError):
    """
    Unknown style of service invocation.

    :ivar service_style: The style requested.
    """
    fmt = 'The service style ({service_style}) is not understood.'


class PaginationError(KSCoreError):
    fmt = 'Error during pagination: {message}'


class OperationNotPageableError(KSCoreError):
    fmt = 'Operation cannot be paginated: {operation_name}'


class ChecksumError(KSCoreError):
    """The expected checksum did not match the calculated checksum.

    """
    fmt = ('Checksum {checksum_type} failed, expected checksum '
           '{expected_checksum} did not match calculated checksum '
           '{actual_checksum}.')


class UnseekableStreamError(KSCoreError):
    """Need to seek a stream, but stream does not support seeking.

    """
    fmt = ('Need to rewind the stream {stream_object}, but stream '
           'is not seekable.')


class WaiterError(KSCoreError):
    """Waiter failed to reach desired state."""
    fmt = 'Waiter {name} failed: {reason}'


class IncompleteReadError(KSCoreError):
    """HTTP response did not return expected number of bytes."""
    fmt = ('{actual_bytes} read, but total bytes '
           'expected is {expected_bytes}.')


class InvalidExpressionError(KSCoreError):
    """Expression is either invalid or too complex."""
    fmt = 'Invalid expression {expression}: Only dotted lookups are supported.'


class UnknownCredentialError(KSCoreError):
    """Tried to insert before/after an unregistered credential type."""
    fmt = 'Credential named {name} not found.'


class WaiterConfigError(KSCoreError):
    """Error when processing waiter configuration."""
    fmt = 'Error processing waiter config: {error_msg}'


class UnknownClientMethodError(KSCoreError):
    """Error when trying to access a method on a client that does not exist."""
    fmt = 'Client does not have method: {method_name}'


class UnsupportedSignatureVersionError(KSCoreError):
    """Error when trying to access a method on a client that does not exist."""
    fmt = 'Signature version is not supported: {signature_version}'


class ClientError(Exception):
    MSG_TEMPLATE = (
        'An error occurred ({error_code}) when calling the {operation_name} '
        'operation: {error_message}')

    def __init__(self, error_response, operation_name):
        if sys.version_info[0] < 3:
            msg = self.MSG_TEMPLATE.format(
                error_code=error_response['Error'].get('Code', 'Unknown'),
                error_message=error_response['Error'].get('Message', 'Unknown').encode("utf-8"),
                operation_name=operation_name)
        else:
            msg = self.MSG_TEMPLATE.format(
                error_code=error_response['Error'].get('Code', 'Unknown'),
                error_message=error_response['Error'].get('Message', 'Unknown'),
                operation_name=operation_name)

        super(ClientError, self).__init__(msg)
        self.response = error_response


class UnsupportedTLSVersionWarning(Warning):
    """Warn when an openssl version that uses TLS 1.2 is required"""
    pass


class ImminentRemovalWarning(Warning):
    pass


class InvalidDNSNameError(KSCoreError):
    """Error when virtual host path is forced on a non-DNS compatible bucket"""
    fmt = (
        'Bucket named {bucket_name} is not DNS compatible. Virtual '
        'hosted-style addressing cannot be used. The addressing style '
        'can be configured by removing the addressing_style value '
        'or setting that value to \'path\' or \'auto\' in the KSYUN Config '
        'file or in the kscore.client.Config object.'
    )


class InvalidS3AddressingStyleError(KSCoreError):
    """Error when an invalid path style is specified"""
    fmt = (
        'S3 addressing style {s3_addressing_style} is invaild. Valid options '
        'are: \'auto\', \'virtual\', and \'path\''
    )


class StubResponseError(KSCoreError):
    fmt = 'Error getting response stub for operation {operation_name}: {reason}'


class StubAssertionError(StubResponseError, AssertionError):
    fmt = 'Error getting response stub for operation {operation_name}: {reason}'


class InvalidConfigError(KSCoreError):
    fmt = '{error_msg}'


class RefreshWithMFAUnsupportedError(KSCoreError):
    fmt = 'Cannot refresh credentials: MFA token required.'


class MD5UnavailableError(KSCoreError):
    fmt = "This system does not support MD5 generation."
