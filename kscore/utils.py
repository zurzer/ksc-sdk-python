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
import re
import logging
import datetime
import hashlib
import binascii
import functools

from six import string_types, text_type
import dateutil.parser
from dateutil.tz import tzlocal, tzutc

from kscore.exceptions import InvalidExpressionError, ConfigNotFound
from kscore.exceptions import InvalidDNSNameError
from kscore.compat import json, quote, zip_longest, urlsplit, urlunsplit
import requests
from kscore.compat import OrderedDict

logger = logging.getLogger(__name__)
DEFAULT_METADATA_SERVICE_TIMEOUT = 1
METADATA_SECURITY_CREDENTIALS_URL = (
    'http://iam.api.ksyun.com/latest/meta-data/iam/security-credentials/'
)
# These are chars that do not need to be urlencoded.
# Based on rfc2986, section 2.3
SAFE_CHARS = '-._~'
LABEL_RE = re.compile('[a-z0-9][a-z0-9\-]*[a-z0-9]')
RESTRICTED_REGIONS = [
    'us-gov-west-1',
    'fips-us-gov-west-1',
]
S3_ACCELERATE_ENDPOINT = 's3.ksyun.com'


class _RetriesExceededError(Exception):
    """Internal exception used when the number of retries are exceeded."""
    pass


def get_service_module_name(service_model):
    """Returns the module name for a service

    This is the value used in both the documentation and client class name
    """
    name = service_model.metadata.get(
        'serviceAbbreviation',
        service_model.metadata.get(
            'serviceFullName', service_model.service_name))

    name = name.replace('AWS', '')
    name = re.sub('\W+', '', name)
    return name


def normalize_url_path(path):
    if not path:
        return '/'
    return remove_dot_segments(path)


def remove_dot_segments(url):
    # RFC 3986, section 5.2.4 "Remove Dot Segments"
    # Also, KSYUN services require consecutive slashes to be removed,
    # so that's done here as well
    if not url:
        return ''
    input_url = url.split('/')
    output_list = []
    for x in input_url:
        if x and x != '.':
            if x == '..':
                if output_list:
                    output_list.pop()
            else:
                output_list.append(x)

    if url[0] == '/':
        first = '/'
    else:
        first = ''
    if url[-1] == '/' and output_list:
        last = '/'
    else:
        last = ''
    return first + '/'.join(output_list) + last


def validate_jmespath_for_set(expression):
    # Validates a limited jmespath expression to determine if we can set a
    # value based on it. Only works with dotted paths.
    if not expression or expression == '.':
        raise InvalidExpressionError(expression=expression)

    for invalid in ['[', ']', '*']:
        if invalid in expression:
            raise InvalidExpressionError(expression=expression)


def set_value_from_jmespath(source, expression, value, is_first=True):
    # This takes a (limited) jmespath-like expression & can set a value based
    # on it.
    # Limitations:
    # * Only handles dotted lookups
    # * No offsets/wildcards/slices/etc.
    if is_first:
        validate_jmespath_for_set(expression)

    bits = expression.split('.', 1)
    current_key, remainder = bits[0], bits[1] if len(bits) > 1 else ''

    if not current_key:
        raise InvalidExpressionError(expression=expression)

    if remainder:
        if current_key not in source:
            # We've got something in the expression that's not present in the
            # source (new key). If there's any more bits, we'll set the key
            # with an empty dictionary.
            source[current_key] = {}

        return set_value_from_jmespath(
            source[current_key],
            remainder,
            value,
            is_first=False
        )

    # If we're down to a single key, set it.
    source[current_key] = value


class InstanceMetadataFetcher(object):
    def __init__(self, timeout=DEFAULT_METADATA_SERVICE_TIMEOUT,
                 num_attempts=1, url=METADATA_SECURITY_CREDENTIALS_URL):
        self._timeout = timeout
        self._num_attempts = num_attempts
        self._url = url

    def _get_request(self, url, timeout, num_attempts=1):
        for i in range(num_attempts):
            try:
                response = requests.get(url, timeout=timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                logger.debug("Caught exception while trying to retrieve "
                             "credentials: %s", e, exc_info=True)
            else:
                if response.status_code == 200:
                    return response
        raise _RetriesExceededError()

    def retrieve_iam_role_credentials(self):
        data = {}
        url = self._url
        timeout = self._timeout
        num_attempts = self._num_attempts
        try:
            r = self._get_request(url, timeout, num_attempts)
            if r.content:
                fields = r.content.decode('utf-8').split('\n')
                for field in fields:
                    if field.endswith('/'):
                        data[field[0:-1]] = self.retrieve_iam_role_credentials(
                            url + field, timeout, num_attempts)
                    else:
                        val = self._get_request(
                            url + field,
                            timeout=timeout,
                            num_attempts=num_attempts).content.decode('utf-8')
                        if val[0] == '{':
                            val = json.loads(val)
                        data[field] = val
            else:
                logger.debug("Metadata service returned non 200 status code "
                             "of %s for url: %s, content body: %s",
                             r.status_code, url, r.content)
        except _RetriesExceededError:
            logger.debug("Max number of attempts exceeded (%s) when "
                         "attempting to retrieve data from metadata service.",
                         num_attempts)
        # We sort for stable ordering. In practice, this should only consist
        # of one role, but may need revisiting if this expands in the future.
        final_data = {}
        for role_name in sorted(data):
            final_data = {
                'role_name': role_name,
                'access_key': data[role_name]['AccessKeyId'],
                'secret_key': data[role_name]['SecretAccessKey'],
                'token': data[role_name]['Token'],
                'expiry_time': data[role_name]['Expiration'],
            }
        return final_data


def merge_dicts(dict1, dict2, append_lists=False):
    """Given two dict, merge the second dict into the first.

    The dicts can have arbitrary nesting.

    :param append_lists: If true, instead of clobbering a list with the new
        value, append all of the new values onto the original list.
    """
    for key in dict2:
        if isinstance(dict2[key], dict):
            if key in dict1 and key in dict2:
                merge_dicts(dict1[key], dict2[key])
            else:
                dict1[key] = dict2[key]
        # If the value is a list and the ``append_lists`` flag is set,
        # append the new values onto the original list
        elif isinstance(dict2[key], list) and append_lists:
            # The value in dict1 must be a list in order to append new
            # values onto it.
            if key in dict1 and isinstance(dict1[key], list):
                dict1[key].extend(dict2[key])
            else:
                dict1[key] = dict2[key]
        else:
            # At scalar types, we iterate and merge the
            # current dict that we're on.
            dict1[key] = dict2[key]


def parse_key_val_file(filename, _open=open):
    try:
        with _open(filename) as f:
            contents = f.read()
            return parse_key_val_file_contents(contents)
    except OSError:
        raise ConfigNotFound(path=filename)


def parse_key_val_file_contents(contents):
    # This was originally extracted from the EC2 credential provider, which was
    # fairly lenient in its parsing.  We only try to parse key/val pairs if
    # there's a '=' in the line.
    final = {}
    for line in contents.splitlines():
        if '=' not in line:
            continue
        key, val = line.split('=', 1)
        key = key.strip()
        val = val.strip()
        final[key] = val
    return final


def percent_encode_sequence(mapping, safe=SAFE_CHARS):
    """Urlencode a dict or list into a string.

    This is similar to urllib.urlencode except that:

    * It uses quote, and not quote_plus
    * It has a default list of safe chars that don't need
      to be encoded, which matches what KSYUN services expect.

    If any value in the input ``mapping`` is a list type,
    then each list element wil be serialized.  This is the equivalent
    to ``urlencode``'s ``doseq=True`` argument.

    This function should be preferred over the stdlib
    ``urlencode()`` function.

    :param mapping: Either a dict to urlencode or a list of
        ``(key, value)`` pairs.

    """
    encoded_pairs = []
    if hasattr(mapping, 'items'):
        pairs = mapping.items()
    else:
        pairs = mapping
    for key, value in pairs:
        if isinstance(value, list):
            for element in value:
                encoded_pairs.append('%s=%s' % (percent_encode(key),
                                                percent_encode(element)))
        else:
            encoded_pairs.append('%s=%s' % (percent_encode(key),
                                            percent_encode(value)))
    return '&'.join(encoded_pairs)


def percent_encode(input_str, safe=SAFE_CHARS):
    """Urlencodes a string.

    Whereas percent_encode_sequence handles taking a dict/sequence and
    producing a percent encoded string, this function deals only with
    taking a string (not a dict/sequence) and percent encoding it.

    """
    if not isinstance(input_str, string_types):
        input_str = text_type(input_str)
    return quote(text_type(input_str).encode('utf-8'), safe=safe)


def parse_timestamp(value):
    """Parse a timestamp into a datetime object.

    Supported formats:

        * iso8601
        * rfc822
        * epoch (value is an integer)

    This will return a ``datetime.datetime`` object.

    """
    if isinstance(value, (int, float)):
        # Possibly an epoch time.
        return datetime.datetime.fromtimestamp(value, tzlocal())
    else:
        try:
            return datetime.datetime.fromtimestamp(float(value), tzlocal())
        except (TypeError, ValueError):
            pass
    try:
        return dateutil.parser.parse(value)
    except (TypeError, ValueError) as e:
        raise ValueError('Invalid timestamp "%s": %s' % (value, e))


def parse_to_aware_datetime(value):
    """Converted the passed in value to a datetime object with tzinfo.

    This function can be used to normalize all timestamp inputs.  This
    function accepts a number of different types of inputs, but
    will always return a datetime.datetime object with time zone
    information.

    The input param ``value`` can be one of several types:

        * A datetime object (both naive and aware)
        * An integer representing the epoch time (can also be a string
          of the integer, i.e '0', instead of 0).  The epoch time is
          considered to be UTC.
        * An iso8601 formatted timestamp.  This does not need to be
          a complete timestamp, it can contain just the date portion
          without the time component.

    The returned value will be a datetime object that will have tzinfo.
    If no timezone info was provided in the input value, then UTC is
    assumed, not local time.

    """
    # This is a general purpose method that handles several cases of
    # converting the provided value to a string timestamp suitable to be
    # serialized to an http request. It can handle:
    # 1) A datetime.datetime object.
    if isinstance(value, datetime.datetime):
        datetime_obj = value
    else:
        # 2) A string object that's formatted as a timestamp.
        #    We document this as being an iso8601 timestamp, although
        #    parse_timestamp is a bit more flexible.
        datetime_obj = parse_timestamp(value)
    if datetime_obj.tzinfo is None:
        # I think a case would be made that if no time zone is provided,
        # we should use the local time.  However, to restore backwards
        # compat, the previous behavior was to assume UTC, which is
        # what we're going to do here.
        datetime_obj = datetime_obj.replace(tzinfo=tzutc())
    else:
        datetime_obj = datetime_obj.astimezone(tzutc())
    return datetime_obj


def datetime2timestamp(dt, default_timezone=None):
    """Calculate the timestamp based on the given datetime instance.

    :type dt: datetime
    :param dt: A datetime object to be converted into timestamp
    :type default_timezone: tzinfo
    :param default_timezone: If it is provided as None, we treat it as tzutc().
                             But it is only used when dt is a naive datetime.
    :returns: The timestamp
    """
    epoch = datetime.datetime(1970, 1, 1)
    if dt.tzinfo is None:
        if default_timezone is None:
            default_timezone = tzutc()
        dt = dt.replace(tzinfo=default_timezone)
    d = dt.replace(tzinfo=None) - dt.utcoffset() - epoch
    if hasattr(d, "total_seconds"):
        return d.total_seconds()  # Works in Python 2.7+
    return (d.microseconds + (d.seconds + d.days * 24 * 3600) * 10 ** 6) / 10 ** 6


def calculate_sha256(body, as_hex=False):
    """Calculate a sha256 checksum.

    This method will calculate the sha256 checksum of a file like
    object.  Note that this method will iterate through the entire
    file contents.  The caller is responsible for ensuring the proper
    starting position of the file and ``seek()``'ing the file back
    to its starting location if other consumers need to read from
    the file like object.

    :param body: Any file like object.  The file must be opened
        in binary mode such that a ``.read()`` call returns bytes.
    :param as_hex: If True, then the hex digest is returned.
        If False, then the digest (as binary bytes) is returned.

    :returns: The sha256 checksum

    """
    checksum = hashlib.sha256()
    for chunk in iter(lambda: body.read(1024 * 1024), b''):
        checksum.update(chunk)
    if as_hex:
        return checksum.hexdigest()
    else:
        return checksum.digest()


def calculate_tree_hash(body):
    """Calculate a tree hash checksum.

    For more information see:

    https://github.com/liuyichen/

    :param body: Any file like object.  This has the same constraints as
        the ``body`` param in calculate_sha256

    :rtype: str
    :returns: The hex version of the calculated tree hash

    """
    chunks = []
    required_chunk_size = 1024 * 1024
    sha256 = hashlib.sha256
    for chunk in iter(lambda: body.read(required_chunk_size), b''):
        chunks.append(sha256(chunk).digest())
    if not chunks:
        return sha256(b'').hexdigest()
    while len(chunks) > 1:
        new_chunks = []
        for first, second in _in_pairs(chunks):
            if second is not None:
                new_chunks.append(sha256(first + second).digest())
            else:
                # We're at the end of the list and there's no pair left.
                new_chunks.append(first)
        chunks = new_chunks
    return binascii.hexlify(chunks[0]).decode('ascii')


def _in_pairs(iterable):
    # Creates iterator that iterates over the list in pairs:
    # for a, b in _in_pairs([0, 1, 2, 3, 4]):
    #     print(a, b)
    #
    # will print:
    # 0, 1
    # 2, 3
    # 4, None
    shared_iter = iter(iterable)
    # Note that zip_longest is a compat import that uses
    # the itertools izip_longest.  This creates an iterator,
    # this call below does _not_ immediately create the list
    # of pairs.
    return zip_longest(shared_iter, shared_iter)


class CachedProperty(object):
    """A read only property that caches the initially computed value.

    This descriptor will only call the provided ``fget`` function once.
    Subsequent access to this property will return the cached value.

    """

    def __init__(self, fget):
        self._fget = fget

    def __get__(self, obj, cls):
        if obj is None:
            return self
        else:
            computed_value = self._fget(obj)
            obj.__dict__[self._fget.__name__] = computed_value
            return computed_value


class ArgumentGenerator(object):
    """Generate sample input based on a shape model.

    This class contains a ``generate_skeleton`` method that will take
    an input shape (created from ``kscore.model``) and generate
    a sample dictionary corresponding to the input shape.

    The specific values used are place holder values. For strings an
    empty string is used, for numbers 0 or 0.0 is used.  The intended
    usage of this class is to generate the *shape* of the input structure.

    This can be useful for operations that have complex input shapes.
    This allows a user to just fill in the necessary data instead of
    worrying about the specific structure of the input arguments.

    Example usage::

        s = kscore.session.get_session()
        ddb = s.get_service_model('dynamodb')
        arg_gen = ArgumentGenerator()
        sample_input = arg_gen.generate_skeleton(
            ddb.operation_model('CreateTable').input_shape)
        print("Sample input for dynamodb.CreateTable: %s" % sample_input)

    """

    def __init__(self):
        pass

    def generate_skeleton(self, shape):
        """Generate a sample input.

        :type shape: ``kscore.model.Shape``
        :param shape: The input shape.

        :return: The generated skeleton input corresponding to the
            provided input shape.

        """
        stack = []
        return self._generate_skeleton(shape, stack)

    def _generate_skeleton(self, shape, stack):
        stack.append(shape.name)
        try:
            if shape.type_name == 'structure':
                return self._generate_type_structure(shape, stack)
            elif shape.type_name == 'list':
                return self._generate_type_list(shape, stack)
            elif shape.type_name == 'map':
                return self._generate_type_map(shape, stack)
            elif shape.type_name == 'string':
                return ''
            elif shape.type_name in ['integer', 'long']:
                return 0
            elif shape.type_name == 'float':
                return 0.0
            elif shape.type_name == 'boolean':
                return True
        finally:
            stack.pop()

    def _generate_type_structure(self, shape, stack):
        if stack.count(shape.name) > 1:
            return {}
        skeleton = OrderedDict()
        for member_name, member_shape in shape.members.items():
            skeleton[member_name] = self._generate_skeleton(member_shape,
                                                            stack)
        return skeleton

    def _generate_type_list(self, shape, stack):
        # For list elements we've arbitrarily decided to
        # return two elements for the skeleton list.
        return [
            self._generate_skeleton(shape.member, stack),
        ]

    def _generate_type_map(self, shape, stack):
        key_shape = shape.key
        value_shape = shape.value
        assert key_shape.type_name == 'string'
        return OrderedDict([
            ('KeyName', self._generate_skeleton(value_shape, stack)),
        ])


def is_valid_endpoint_url(endpoint_url):
    """Verify the endpoint_url is valid.

    :type endpoint_url: string
    :param endpoint_url: An endpoint_url.  Must have at least a scheme
        and a hostname.

    :return: True if the endpoint url is valid. False otherwise.

    """
    parts = urlsplit(endpoint_url)
    hostname = parts.hostname
    if hostname is None:
        return False
    if len(hostname) > 255:
        return False
    if hostname[-1] == ".":
        hostname = hostname[:-1]
    allowed = re.compile(
        "^((?!-)[A-Z\d-]{1,63}(?<!-)\.)*((?!-)[A-Z\d-]{1,63}(?<!-))$",
        re.IGNORECASE)
    return allowed.match(hostname)


def check_dns_name(bucket_name):
    """
    Check to see if the ``bucket_name`` complies with the
    restricted DNS naming conventions necessary to allow
    access via virtual-hosting style.

    Even though "." characters are perfectly valid in this DNS
    naming scheme, we are going to punt on any name containing a
    "." character because these will cause SSL cert validation
    problems if we try to use virtual-hosting style addressing.
    """
    if '.' in bucket_name:
        return False
    n = len(bucket_name)
    if n < 3 or n > 63:
        # Wrong length
        return False
    if n == 1:
        if not bucket_name.isalnum():
            return False
    match = LABEL_RE.match(bucket_name)
    if match is None or match.end() != len(bucket_name):
        return False
    return True


def fix_s3_host(request, signature_version, region_name, **kwargs):
    """
    This handler looks at S3 requests just before they are signed.
    If there is a bucket name on the path (true for everything except
    ListAllBuckets) it checks to see if that bucket name conforms to
    the DNS naming conventions.  If it does, it alters the request to
    use ``virtual hosting`` style addressing rather than ``path-style``
    addressing.  This allows us to avoid 301 redirects for all
    bucket names that can be CNAME'd.
    """
    # By default we do not use virtual hosted style addressing when
    # signed with signature version 4.
    if signature_version in ['s3v4', 'v4']:
        return
    elif not _allowed_region(region_name):
        return
    try:
        switch_to_virtual_host_style(
            request, signature_version, 's3.ksyun.com')
    except InvalidDNSNameError as e:
        bucket_name = e.kwargs['bucket_name']
        logger.debug('Not changing URI, bucket is not DNS compatible: %s',
                     bucket_name)


def switch_to_virtual_host_style(request, signature_version,
                                 default_endpoint_url=None, **kwargs):
    """
    This is a handler to force virtual host style s3 addressing no matter
    the signature version (which is taken in consideration for the default
    case). If the bucket is not DNS compatible an InvalidDNSName is thrown.

    :param request: A KSRequest object that is about to be sent.
    :param signature_version: The signature version to sign with
    :param default_endpoint_url: The endpoint to use when switching to a
        virtual style. If None is supplied, the virtual host will be
        constructed from the url of the request.
    """
    if request.auth_path is not None:
        # The auth_path has already been applied (this may be a
        # retried request).  We don't need to perform this
        # customization again.
        return
    elif _is_get_bucket_location_request(request):
        # For the GetBucketLocation response, we should not be using
        # the virtual host style addressing so we can avoid any sigv4
        # issues.
        logger.debug("Request is GetBucketLocation operation, not checking "
                     "for DNS compatibility.")
        return
    parts = urlsplit(request.url)
    request.auth_path = parts.path
    path_parts = parts.path.split('/')

    # Retrieve what the endpoint we will be prepending the bucket name to.
    if default_endpoint_url is None:
        default_endpoint_url = parts.netloc

    if len(path_parts) > 1:
        bucket_name = path_parts[1]
        if not bucket_name:
            # If the bucket name is empty we should not be checking for
            # dns compatibility.
            return
        logger.debug('Checking for DNS compatible bucket for: %s',
                     request.url)
        if check_dns_name(bucket_name):
            # If the operation is on a bucket, the auth_path must be
            # terminated with a '/' character.
            if len(path_parts) == 2:
                if request.auth_path[-1] != '/':
                    request.auth_path += '/'
            path_parts.remove(bucket_name)
            # At the very least the path must be a '/', such as with the
            # CreateBucket operation when DNS style is being used. If this
            # is not used you will get an empty path which is incorrect.
            path = '/'.join(path_parts) or '/'
            global_endpoint = default_endpoint_url
            host = bucket_name + '.' + global_endpoint
            new_tuple = (parts.scheme, host, path,
                         parts.query, '')
            new_uri = urlunsplit(new_tuple)
            request.url = new_uri
            logger.debug('URI updated to: %s', new_uri)
        else:
            raise InvalidDNSNameError(bucket_name=bucket_name)


def _is_get_bucket_location_request(request):
    return request.url.endswith('?location')


def _allowed_region(region_name):
    return region_name not in RESTRICTED_REGIONS


def instance_cache(func):
    """Method decorator for caching method calls to a single instance.

    **This is not a general purpose caching decorator.**

    In order to use this, you *must* provide an ``_instance_cache``
    attribute on the instance.

    This decorator is used to cache method calls.  The cache is only
    scoped to a single instance though such that multiple instances
    will maintain their own cache.  In order to keep things simple,
    this decorator requires that you provide an ``_instance_cache``
    attribute on your instance.

    """
    func_name = func.__name__

    @functools.wraps(func)
    def _cache_guard(self, *args, **kwargs):
        cache_key = (func_name, args)
        if kwargs:
            kwarg_items = tuple(sorted(kwargs.items()))
            cache_key = (func_name, args, kwarg_items)
        result = self._instance_cache.get(cache_key)
        if result is not None:
            return result
        result = func(self, *args, **kwargs)
        self._instance_cache[cache_key] = result
        return result

    return _cache_guard


def switch_host_s3_accelerate(request, operation_name, **kwargs):
    """Switches the current s3 endpoint with an S3 Accelerate endpoint"""

    # Note that when registered the switching of the s3 host happens
    # before it gets changed to virtual. So we are not concerned with ensuring
    # that the bucket name is translated to the virtual style here and we
    # can hard code the Accelerate endpoint.
    endpoint = 'https://' + S3_ACCELERATE_ENDPOINT
    if operation_name in ['ListBuckets', 'CreateBucket', 'DeleteBucket']:
        return
    _switch_hosts(request, endpoint, use_new_scheme=False)


def switch_host_with_param(request, param_name):
    """Switches the host using a parameter value from a JSON request body"""
    request_json = json.loads(request.data.decode('utf-8'))
    if request_json.get(param_name):
        new_endpoint = request_json[param_name]
        _switch_hosts(request, new_endpoint)


def _switch_hosts(request, new_endpoint, use_new_scheme=True):
    new_endpoint_components = urlsplit(new_endpoint)
    original_endpoint = request.url
    original_endpoint_components = urlsplit(original_endpoint)
    scheme = original_endpoint_components.scheme
    if use_new_scheme:
        scheme = new_endpoint_components.scheme
    final_endpoint_components = (
        scheme,
        new_endpoint_components.netloc,
        original_endpoint_components.path,
        original_endpoint_components.query,
        ''
    )
    final_endpoint = urlunsplit(final_endpoint_components)
    logger.debug('Updating URI from %s to %s' % (request.url, final_endpoint))
    request.url = final_endpoint


def set_logger_level(level=logging.DEBUG):
    for name in logging.Logger.manager.loggerDict.keys():
        if name.find('kscore') == 0:
            logging.getLogger(name).setLevel(level)
