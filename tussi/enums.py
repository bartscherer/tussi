from enum import StrEnum


class CommonHeader(StrEnum):
    '''
        HTTP headers relevant for but not exclusively for TUS
        (as defined by https://tus.io/protocols/resumable-upload)
    '''

    CACHE_CONTROL = 'Cache-Control'
    '''
        It's recommended to set the Cache-Control header to no-store for HEAD
        requests to prevent offset from being cached
    '''

    CONTENT_LENGTH = 'Content-Length'
    '''
        The Content-Length specifies the current chunk size for file data
        uploads (patch)
    '''

    CONTENT_TYPE = 'Content-Type'
    '''
        The Content-Type MUST be 'application/offset+octet-stream' for PATCH
        requests.
    '''

    LOCATION = 'Location'
    '''
        The location header will be set by the POST method which creates a file
        upload in order to direct the client to the correct upload id
    '''

    X_HTTP_METHOD_OVERRIDE = 'X-HTTP-Method-Override'
    '''
        The X-HTTP-Method-Override request header MUST be a string which MUST
        be interpreted as the request's method by the Server, if the header is
        presented. The actual method of the request MUST be ignored.
        The Client SHOULD use this header if its environment does not support
        the PATCH or DELETE methods.
    '''


class TUSHeader(StrEnum):
    '''
        HTTP headers relevant for TUS
        (as defined by https://tus.io/protocols/resumable-upload)
    '''

    UPLOAD_OFFSET = 'Upload-Offset'
    '''
        The Upload-Offset request and response header indicates a byte offset
        within a resource. The value MUST be a non-negative integer.
    '''

    UPLOAD_METADATA = 'Upload-Metadata'
    '''
        The Upload-Metadata request and response header MUST consist of one
        or more comma-separated key-value pairs. The key and value MUST be
        separated by a space. The key MUST NOT contain spaces and commas and
        MUST NOT be empty.
        The key SHOULD be ASCII encoded and the value MUST be Base64 encoded.
        All keys MUST be unique. The value MAY be empty. In these cases,
        the space, which would normally separate the key and the value, MAY
        be left out.
    '''

    UPLOAD_LENGTH = 'Upload-Length'
    '''
        The Upload-Length request and response header indicates the size of
        the entire upload in bytes. The value MUST be a non-negative integer.
    '''

    TUS_VERSION = 'Tus-Version'
    '''
        The Tus-Version response header MUST be a comma-separated list of
        protocol versions supported by the Server. The list MUST be sorted
        by Server's preference where the first one is the most preferred one.
    '''

    TUS_RESUMABLE = 'Tus-Resumable'
    '''
        The Tus-Resumable header MUST be included in every request and response
        except for OPTIONS requests. The value MUST be the version of the
        protocol used by the Client or the Server.
        If the version specified by the Client is not supported by the Server,
        it MUST respond with the 412 Precondition Failed status and MUST
        include the Tus-Version header into the response.
        In addition, the Server MUST NOT process the request.
    '''

    TUS_EXTENSION = 'Tus-Extension'
    '''
        The Tus-Extension response header MUST be a comma-separated list of the
        extensions supported by the Server. If no extensions are supported,
        the Tus-Extension header MUST be omitted.
    '''

    TUS_MAX_SIZE = 'Tus-Max-Size'
    '''
        The Tus-Max-Size response header MUST be a non-negative integer
        indicating the maximum allowed size of an entire upload in bytes.
        The Server SHOULD set this header if there is a known hard limit.
    '''
