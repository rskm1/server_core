import logging
import os
import tinys3
import urllib
from nose.tools import set_trace
from sqlalchemy.orm.session import Session
from urlparse import urlsplit
from util.mirror import MirrorUploader

from config import CannotLoadConfiguration
from requests.exceptions import (
    ConnectionError,
    HTTPError,
)

class S3Uploader(MirrorUploader):


    BOOK_COVERS_BUCKET_KEY = u'book_covers_bucket'
    OA_CONTENT_BUCKET_KEY = u'open_access_content_bucket'

    S3_HOSTNAME = "s3.amazonaws.com"
    S3_BASE = "http://%s/" % S3_HOSTNAME

    UNINITIALIZED_BUCKETS = object()
    __buckets__ = UNINITIALIZED_BUCKETS

    @classmethod
    def from_config(cls, _db):
        """Create an S3Uploader from site configuration.

        :return: An S3Uploader.
        :raise: CannotLoadConfiguration if S3 is not configured.
        """
        from config import CannotLoadConfiguration
        integration = cls.integration(_db)
        if not integration:
            raise CannotLoadConfiguration(
                "Required S3 integration is not configured."
            )
            return None
        cls.initialize_buckets(_db)        
        return cls(integration.username, integration.password)
        
    def __init__(self, access_key=None, secret_key=None, pool=None):
        from config import CannotLoadConfiguration
        self.pool = pool
        if not self.pool:
            if not (access_key and secret_key):
                raise CannotLoadConfiguration(
                    'Cannot create S3Uploader without both'
                    ' access_key and secret_key.'
                )
            self.pool = tinys3.Pool(access_key, secret_key)

    @classmethod
    def integration(cls, _db):
        """Find the ExternalIntegration that configures this site's S3
        uploader.

        :return: None if there is no S3 configuration, otherwise an ExternalIntegration.
        :raise: CannotLoadConfiguration if there are multiple S3 configurations.
        """
        from model import ExternalIntegration as EI
        integrations = _db.query(EI).filter(
            EI.protocol==EI.S3, EI.goal==EI.STORAGE_GOAL).all()

        from config import CannotLoadConfiguration
        if not integrations:
            return None
        
        if len(integrations) > 1:
            # Right now the S3Uploader doesn't distinguish usage between
            # S3 accounts. If two account integrations are found, raise
            # an error.
            raise CannotLoadConfiguration(
                'Multiple S3 ExternalIntegrations configured'
            )

        return integrations[0]

    @classmethod
    def initialize_buckets(cls, _db):
        integration = cls.integration(_db)
        if not integration:
            # No integration means nothing to initialize.
            return
        cls.__buckets__ = dict()
        for setting in integration.settings:
            if setting.key.endswith('_bucket'):
                cls.__buckets__[setting.key] = setting.value

    @classmethod
    def get_bucket(cls, bucket_key, sessioned_object=None):
        """Gets the bucket for a particular use based on the given key"""
        if cls.__buckets__ == cls.UNINITIALIZED_BUCKETS:
            if not sessioned_object:
                raise CannotLoadConfiguration(
                    'S3 buckets have not been initialized and no'
                    ' database session is available')
            _db = Session.object_session(sessioned_object)
            cls.initialize_buckets(_db)

        if not cls.__buckets__ or not cls.__buckets__.get(bucket_key):
            raise CannotLoadConfiguration(
                "No S3 bucket found for '%s'. Use S3Uploader.from_config"
                " to load S3 bucket settings from database." % bucket_key)

        return cls.__buckets__.get(bucket_key)

    @classmethod
    def url(cls, bucket, path):
        """The URL to a resource on S3 identified by bucket and path."""
        if path.startswith('/'):
            path = path[1:]
        if bucket.startswith('http://') or bucket.startswith('https://'):
            url = bucket
        else:
            url = cls.S3_BASE + bucket
        if not url.endswith('/'):
            url += '/'
        return url + path

    @classmethod
    def cover_image_root(cls, bucket, data_source, scaled_size=None):
        """The root URL to the S3 location of cover images for
        the given data source.
        """
        if scaled_size:
            path = "/scaled/%d/" % scaled_size
        else:
            path = "/"
        if isinstance(data_source, str):
            data_source_name = data_source
        else:
            data_source_name = data_source.name
        data_source_name = urllib.quote(data_source_name)
        path += data_source_name + "/"
        url = cls.url(bucket, path)
        if not url.endswith('/'):
            url += '/'
        return url

    @classmethod
    def content_root(cls, bucket, open_access=True):
        """The root URL to the S3 location of hosted content of
        the given type.
        """
        if not open_access:
            raise NotImplementedError()
        return cls.url(bucket, '/')

    @classmethod
    def book_url(cls, identifier, extension='.epub', open_access=True, 
                 data_source=None, title=None):
        """The path to the hosted EPUB file for the given identifier."""
        bucket = cls.get_bucket(cls.OA_CONTENT_BUCKET_KEY, identifier)
        root = cls.content_root(bucket, open_access)

        if not extension.startswith('.'):
            extension = '.' + extension

        if title:
            filename = "%s/%s" % (identifier.identifier, title)
        else:
            filename = identifier.identifier

        args = [identifier.type, filename]
        args = [urllib.quote(x.encode('utf-8')) for x in args]
        if data_source:
            args.insert(0, urllib.quote(data_source.name))
            template = "%s/%s/%s%s"
        else:
            template = "%s/%s%s"

        return root + template % tuple(args + [extension])

    @classmethod
    def cover_image_url(cls, data_source, identifier, filename=None,
                        scaled_size=None):
        """The path to the hosted cover image for the given identifier."""
        bucket = cls.get_bucket(cls.BOOK_COVERS_BUCKET_KEY, identifier)
        root = cls.cover_image_root(bucket, data_source, scaled_size)

        args = [identifier.type, identifier.identifier, filename]
        args = [urllib.quote(x) for x in args]
        return root + "%s/%s/%s" % tuple(args)

    @classmethod
    def bucket_and_filename(cls, url):
        scheme, netloc, path, query, fragment = urlsplit(url)
        if netloc == 's3.amazonaws.com':
            if path.startswith('/'):
                path = path[1:]
            bucket, filename = path.split("/", 1)
        else:
            bucket = netloc
            filename = path[1:]        
        return bucket, filename

    def mirror_one(self, representation):
        """Mirror a single representation."""
        return self.mirror_batch([representation])

    def mirror_batch(self, representations):
        """Mirror a bunch of Representations at once."""
        filehandles = []
        requests = []
        representations_by_response_url = dict()
        for representation in representations:
            if not representation.mirror_url:
                representation.mirror_url = representation.url
            # Turn the mirror URL into an s3.amazonaws.com URL.
            bucket, filename = self.bucket_and_filename(
                representation.mirror_url
            )
            response_url = self.url(bucket, filename)
            representations_by_response_url[response_url] = (
                representation)
            media_type = representation.external_media_type
            fh = representation.external_content()
            bucket, remote_filename = self.bucket_and_filename(
                representation.mirror_url)
            filehandles.append(fh)
            request = self.pool.upload(
                remote_filename, fh, bucket=bucket,
                content_type=media_type
            )
            requests.append(request)
        # Do the upload.

        def process_response(response):
            representation = representations_by_response_url[response.url]
            if response.status_code == 200:
                source = representation.local_content_path
                if representation.url != representation.mirror_url:
                    source = representation.url
                if source:
                    logging.info("MIRRORED %s => %s",
                                 source, representation.mirror_url)
                else:
                    logging.info("MIRRORED %s", representation.mirror_url)
                representation.set_as_mirrored()
            else:
                representation.mirrored_at = None
                representation.mirror_exception = "Status code %d: %s" % (
                    response.status_code, response.content)

        try:
            for response in self.pool.as_completed(requests):
                process_response(response)
        except ConnectionError, e:
            # This is a transient error; we can just try again.
            logging.error("S3 connection error: %r", e, exc_info=e)
            pass
        except HTTPError, e:
            # Probably also a transient error. In any case
            # there's nothing we can do about it but try again.
            logging.error("S3 HTTP error: %r", e, exc_info=e)
            pass

        # Close the filehandles
        for fh in filehandles:
            fh.close()


class DummyS3Uploader(S3Uploader):
    """A dummy uploader for use in tests."""

    __buckets__ = {
       S3Uploader.BOOK_COVERS_BUCKET_KEY : 'test.cover.bucket',
       S3Uploader.OA_CONTENT_BUCKET_KEY : 'test.content.bucket',
    }

    def __init__(self, fail=False, *args, **kwargs):
        self.uploaded = []
        self.content = []
        self.fail = fail

    def mirror_batch(self, representations):
        self.uploaded.extend(representations)
        self.content.extend([r.content for r in representations])
        for representation in representations:
            if self.fail:
                representation.mirror_exception = "Exception"
                representation.mirrored_at = None
            else:
                if not representation.mirror_url:
                    representation.mirror_url = representation.url
                representation.set_as_mirrored()

class MockS3Response(object):
    def __init__(self, url):
        self.url = url

class MockS3Pool(object):
    """This pool lets us test the real S3Uploader class with a mocked-up S3
    pool.
    """

    def __init__(self):
        self.uploads = []
        self.in_progress = []
        self.n = 0

    def upload(self, remote_filename, fh, bucket=None, content_type=None,
               **kwargs):
        self.uploads.append((remote_filename, fh.read(), bucket, content_type, 
                             kwargs))
        # TODO: Instead of generating a fake URL we should be able to
        # generate the same URL the s3 module would generate in this
        # situation. Without this, we can't properly test the code at the
        # end of mirror_batch which calls process_response.
        response = MockS3Response("http://s3/%s" % self.n)
        self.n += 1
        self.in_progress = []
        return response

    def as_completed(self, requests):
        for i in self.in_progress:
            yield i
        self.in_progress = []
