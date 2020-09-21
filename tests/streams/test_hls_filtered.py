import requests_mock
from tests.mock import MagicMock, call, patch
import unittest

import itertools
from textwrap import dedent
from threading import Event, Thread

from streamlink.session import Streamlink
from streamlink.stream.hls import HLSStream
from streamlink.stream.hls_filtered import FilteredHLSStreamWriter, FilteredHLSStreamReader


class _TestSubjectFilteredHLSStreamWriter(FilteredHLSStreamWriter):
    def __init__(self, *args, **kwargs):
        super(_TestSubjectFilteredHLSStreamWriter, self).__init__(*args, **kwargs)
        self.write_wait = Event()
        self.write_done = Event()

    def write(self, *args, **kwargs):
        # only write once per step
        self.write_wait.wait()
        self.write_wait.clear()

        # don't write again during cleanup
        if self.closed:
            return

        super(_TestSubjectFilteredHLSStreamWriter, self).write(*args, **kwargs)

        # notify main thread that writing has finished
        self.write_done.set()


class _TestSubjectFilteredHLSReader(FilteredHLSStreamReader):
    __writer__ = _TestSubjectFilteredHLSStreamWriter


class _TestSubjectReadThread(Thread):
    """
    Run the reader on a separate thread, so that each read can be controlled from within the main thread
    """
    def __init__(self, segments, playlists):
        Thread.__init__(self)
        self.daemon = True

        self.mocks = mocks = {}
        self.mock = requests_mock.Mocker()
        self.mock.start()

        def addmock(method, url, *args, **kwargs):
            mocks[url] = method(url, *args, **kwargs)

        addmock(self.mock.get, TestFilteredHLSStream.url_playlist, [{"text": p} for p in playlists])
        for i, segment in enumerate(segments):
            addmock(self.mock.get, TestFilteredHLSStream.url_segment.format(i), content=segment)

        session = Streamlink()
        session.set_option("hls-live-edge", 2)
        session.set_option("hls-timeout", 0)
        session.set_option("stream-timeout", 0)

        self.read_wait = Event()
        self.read_done = Event()
        self.data = []
        self.error = None

        self.stream = HLSStream(session, TestFilteredHLSStream.url_playlist)
        self.reader = _TestSubjectFilteredHLSReader(self.stream)
        self.reader.open()

    def run(self):
        while True:
            # only read once per step
            self.read_wait.wait()
            self.read_wait.clear()

            # don't read again during cleanup
            if self.reader.closed:
                return

            try:
                data = self.reader.read(-1)
                self.data.append(data)
            except IOError as err:
                self.error = err
                return
            finally:
                # notify main thread that reading has finished
                self.read_done.set()

    def cleanup(self):
        self.reader.close()
        self.mock.stop()
        # make sure that write and read threads halts on cleanup
        self.reader.writer.write_wait.set()
        self.read_wait.set()

    def await_write(self):
        writer = self.reader.writer
        if not writer.closed:
            # make one write call and wait until write call has finished
            writer.write_wait.set()
            writer.write_done.wait()
            writer.write_done.clear()

    def await_read(self):
        if not self.reader.closed:
            # make one read call and wait until read call has finished
            self.read_wait.set()
            self.read_done.wait()
            self.read_done.clear()


@patch("streamlink.stream.hls.HLSStreamWorker.wait", MagicMock(return_value=True))
class TestFilteredHLSStream(unittest.TestCase):
    url_playlist = "http://mocked/path/playlist.m3u8"
    url_segment = "http://mocked/path/stream{0}.ts"

    @classmethod
    def get_segments(cls, num):
        return ["[{0}]".format(i).encode("ascii") for i in range(num)]

    @classmethod
    def get_playlist(cls, media_sequence, items, filtered=False, end=False):
        playlist = dedent("""
            #EXTM3U
            #EXT-X-VERSION:5
            #EXT-X-TARGETDURATION:1
            #EXT-X-MEDIA-SEQUENCE:{0}
        """.format(media_sequence))

        for item in items:
            playlist += "#EXTINF:1.000,{1}\nstream{0}.ts\n".format(item, "filtered" if filtered else "live")

        if end:
            playlist += "#EXT-X-ENDLIST\n"

        return playlist

    @classmethod
    def filter_sequence(cls, sequence):
        return sequence.segment.title == "filtered"

    def subject(self, segments, playlists):
        thread = _TestSubjectReadThread(segments, playlists)
        self.addCleanup(thread.cleanup)
        thread.start()

        return thread, thread.reader, thread.reader.writer

    @patch("streamlink.stream.hls_filtered.FilteredHLSStreamWriter.should_filter_sequence", new=filter_sequence)
    @patch("streamlink.stream.hls_filtered.log")
    def test_filtered_logging(self, mock_log):
        segments = self.get_segments(8)
        thread, reader, writer = self.subject(segments, [
            self.get_playlist(0, [0, 1], filtered=True),
            self.get_playlist(2, [2, 3], filtered=False),
            self.get_playlist(4, [4, 5], filtered=True),
            self.get_playlist(6, [6, 7], filtered=False, end=True)
        ])

        self.assertTrue(reader.filter_event.is_set(), "Doesn't let the reader wait if not filtering")

        for i in range(2):
            thread.await_write()
            thread.await_write()
            self.assertEqual(len(mock_log.info.mock_calls), i * 2 + 1)
            self.assertEqual(mock_log.info.mock_calls[i * 2 + 0], call("Filtering out segments and pausing stream output"))
            self.assertFalse(reader.filter_event.is_set(), "Lets the reader wait if filtering")

            thread.await_write()
            thread.await_write()
            self.assertEqual(len(mock_log.info.mock_calls), i * 2 + 2)
            self.assertEqual(mock_log.info.mock_calls[i * 2 + 1], call("Resuming stream output"))
            self.assertTrue(reader.filter_event.is_set(), "Doesn't let the reader wait if not filtering")

            thread.await_read()

        self.assertEqual(
            b"".join(thread.data),
            b"".join(list(itertools.chain(segments[2:4], segments[6:8]))),
            "Correctly filters out segments"
        )
        for i, _ in enumerate(segments):
            self.assertTrue(thread.mocks[TestFilteredHLSStream.url_segment.format(i)].called, "Downloads all segments")

    # don't patch should_filter_sequence here (it always returns False)
    def test_not_filtered(self):
        segments = self.get_segments(2)
        thread, reader, writer = self.subject(segments, [
            self.get_playlist(0, [0, 1], filtered=True, end=True)
        ])

        thread.await_write()
        thread.await_write()
        thread.await_read()
        self.assertEqual(b"".join(thread.data), b"".join(segments[0:2]), "Does not filter by default")

    @patch("streamlink.stream.hls_filtered.FilteredHLSStreamWriter.should_filter_sequence", new=filter_sequence)
    def test_filtered_timeout(self):
        segments = self.get_segments(2)
        thread, reader, writer = self.subject(segments, [
            self.get_playlist(0, [0, 1], filtered=False, end=True)
        ])

        thread.await_write()
        thread.await_read()
        self.assertEqual(thread.data, segments[0:1], "Has read the first segment")

        # simulate a timeout by having an empty buffer
        # timeout value is set to 0
        thread.await_read()
        self.assertIsInstance(thread.error, IOError, "Raises a timeout error when no data is available to read")

    @patch("streamlink.stream.hls_filtered.FilteredHLSStreamWriter.should_filter_sequence", new=filter_sequence)
    def test_filtered_no_timeout(self):
        segments = self.get_segments(4)
        thread, reader, writer = self.subject(segments, [
            self.get_playlist(0, [0, 1], filtered=True),
            self.get_playlist(2, [2, 3], filtered=False, end=True)
        ])

        self.assertTrue(reader.filter_event.is_set(), "Doesn't let the reader wait if not filtering")

        thread.await_write()
        thread.await_write()
        self.assertFalse(reader.filter_event.is_set(), "Lets the reader wait if filtering")

        # make reader read (no data available yet)
        thread.read_wait.set()
        # once data becomes available, the reader continues reading
        thread.await_write()
        self.assertTrue(reader.filter_event.is_set(), "Reader is not waiting anymore")

        thread.read_done.wait()
        thread.read_done.clear()
        self.assertFalse(thread.error, "Doesn't time out when filtering")
        self.assertEqual(thread.data, segments[2:3], "Reads next available buffer data")

        thread.await_write()
        thread.await_read()
        self.assertEqual(thread.data, segments[2:4])

    @patch("streamlink.stream.hls_filtered.FilteredHLSStreamWriter.should_filter_sequence", new=filter_sequence)
    def test_filtered_closed(self):
        segments = self.get_segments(2)
        thread, reader, writer = self.subject(segments, [
            self.get_playlist(0, [0, 1], filtered=True)
        ])

        self.assertTrue(reader.filter_event.is_set(), "Doesn't let the reader wait if not filtering")
        thread.await_write()
        self.assertFalse(reader.filter_event.is_set(), "Lets the reader wait if filtering")

        # make reader read (no data available yet)
        thread.read_wait.set()

        # close stream while reader is waiting for filtering to end
        thread.reader.close()
        thread.read_done.wait()
        thread.read_done.clear()
        self.assertEqual(thread.data, [b""], "Stops reading on stream close")
