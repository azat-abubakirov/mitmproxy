import six
from six.moves import urllib

from netlib import utils


# PY2 workaround
def decode_parse_result(result, enc):
    if hasattr(result, "decode"):
        return result.decode(enc)
    else:
        return urllib.parse.ParseResult(*[x.decode(enc) for x in result])


# PY2 workaround
def encode_parse_result(result, enc):
    if hasattr(result, "encode"):
        return result.encode(enc)
    else:
        return urllib.parse.ParseResult(*[x.encode(enc) for x in result])


def parse(url):
    """
        URL-parsing function that checks that
            - port is an integer 0-65535
            - host is a valid IDNA-encoded hostname with no null-bytes
            - path is valid ASCII

        Args:
            A URL (as bytes or as unicode)

        Returns:
            A (scheme, host, port, path) tuple

        Raises:
            ValueError, if the URL is not properly formatted.
    """
    parsed = urllib.parse.urlparse(url)

    if not parsed.hostname:
        raise ValueError("No hostname given")

    if isinstance(url, six.binary_type):
        host = parsed.hostname

        # this should not raise a ValueError,
        # but we try to be very forgiving here and accept just everything.
        # decode_parse_result(parsed, "ascii")
    else:
        host = parsed.hostname.encode("idna")
        parsed = encode_parse_result(parsed, "ascii")

    port = parsed.port
    if not port:
        port = 443 if parsed.scheme == b"https" else 80

    full_path = urllib.parse.urlunparse(
        (b"", b"", parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
    if not full_path.startswith(b"/"):
        full_path = b"/" + full_path

    if not utils.is_valid_host(host):
        raise ValueError("Invalid Host")
    if not utils.is_valid_port(port):
        raise ValueError("Invalid Port")

    return parsed.scheme, host, port, full_path


def unparse(scheme, host, port, path=""):
    """
    Returns a URL string, constructed from the specified components.

    Args:
        All args must be str.
    """
    if path == "*":
        path = ""
    return "%s://%s%s" % (scheme, utils.hostport(scheme, host, port), path)


def encode(s):
    """
        Takes a list of (key, value) tuples and returns a urlencoded string.
    """
    s = [tuple(i) for i in s]
    return urllib.parse.urlencode(s, False)


def decode(s):
    """
        Takes a urlencoded string and returns a list of (key, value) tuples.
    """
    return urllib.parse.parse_qsl(s, keep_blank_values=True)