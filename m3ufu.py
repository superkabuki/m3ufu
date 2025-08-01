"""
 m3ufu
"""
import argparse
from collections import deque
import json
import os
import sys
import time
import pyaes
import threefive
from threefive import reader, TagParser

"""
Odd number versions are releases.
Even number versions are testing builds between releases.

Used to set version in setup.py
and as an easy way to check which
version you have installed.
"""

MAJOR = "0"
MINOR = "0"
MAINTAINENCE = "97"


def version():
    """
    version prints the m3ufu version as a string
    """
    return f"{MAJOR}.{MINOR}.{MAINTAINENCE}"


BASIC_TAGS = (
    "#EXTM3U",
    "#EXT-X-VERSION",
    "#EXT-X-ALLOW-CACHE",
)

MULTI_TAGS = (
    "#EXT-X-INDEPENDENT-SEGMENTS",
    "#EXT-X-START",
    "#EXT-X-DEFINE",
)

MEDIA_TAGS = (
    "#EXT-X-TARGETDURATION",
    "#EXT-X-MEDIA-SEQUENCE",
    "#EXT-X-DISCONTINUITY-SEQUENCE",
    "#EXT-X-PLAYLIST-TYPE",
    "#EXT-X-I-FRAMES-ONLY",
    "#EXT-X-PART-INF",
    "EXT-X-SERVER-CONTROL",
)

SEGMENT_TAGS = (
    "#EXT-X-PUBLISHED-TIME",
    "#EXT-X-PROGRAM-DATE-TIME",
)

HEADER_TAGS = BASIC_TAGS + MULTI_TAGS + MEDIA_TAGS  # + SEGMENT_TAGS


def atoif(value):
    """
    atoif converts ascii to (int|float)
    """
    if "." in value:
        try:
            value = float(value)
        finally:
            return value
    else:
        try:
            value = int(value)
        finally:
            return value


class AESDecrypt:
    """
    AESDecrypt decrypts AES encrypted segments
    and returns a file path to the converted segment.
    """

    def __init__(self, seg_uri, key_uri, iv):
        self.seg_uri = seg_uri
        self.key_uri = key_uri
        self.key = None
        self.iv = None
        self.media = None
        self._mk_media()
        self.iv = int.to_bytes(int(iv, 16), 16, byteorder="big")
        self._aes_get_key()

    def _mk_media(self):
        self.media = "noaes-"
        self.media += self.seg_uri.rsplit("/", 1)[-1]

    def _aes_get_key(self):
        with reader(self.key_uri) as quay:
            self.key = quay.read()

    def decrypt(self):
        mode = pyaes.AESModeOfOperationCBC(self.key, iv=self.iv)
        with open(self.media, "wb") as outfile, reader(self.seg_uri) as infile:
            pyaes.decrypt_stream(mode, infile, outfile)
        return self.media


class HlsSegment:
    """
    The HlsSegment class represents a segment
    and associated data
    """

    def __init__(self, lines, media_uri, start, base_uri):
        self.lines = lines
        self.media = media_uri
        self.pts = None
        self.start = start
        self.end = None
        self.duration = 0
        self.cue = False
        self.cue_data = None
        self.tags = {}
        self.tmp = None
        self.base_uri = base_uri
        self.relative_uri = media_uri.replace(base_uri, "")
        self.last_iv = None
        self.last_key_uri = None
        self.debug = False

    def __repr__(self):
        return str(self.__dict__)

    @staticmethod
    def _dot_dot(media_uri):
        """
        dot dot resolves '..' in  urls
        """
        ssu = media_uri.split("/")
        ss, u = ssu[:-1], ssu[-1:]
        while ".." in ss:
            i = ss.index("..")
            del ss[i]
            del ss[i - 1]
        media_uri = "/".join(ss + u)
        return media_uri

    def kv_clean(self):
        """
        _kv_clean removes items from a dict if the value is None
        """

        def b2l(val):
            if isinstance(val, (list)):
                val = [b2l(v) for v in val]
            if isinstance(val, (dict)):
                val = {k: b2l(v) for k, v in val.items()}
            return val

        return {k: b2l(v) for k, v in vars(self).items() if v}

    def _get_pts_start(self):
        try:
            strm = threefive.Segment(self.media_file())
            strm.decode(func=None)
            pts_start = strm.pts_start
            #  print(pts_start)
            self.pts = round(pts_start, 6)
            self.start = self.pts
        except:
            pass

    def media_file(self):
        """
        media_file returns self.media
        or self.tmp if self.media is AES Encrypted
        """
        media_file = self.media
        if self.tmp:
            media_file = self.tmp
        return media_file

    def desegment(self, outfile):
        with reader(self.media_file()) as infile:
            data = infile.read()
        with open(outfile, "ab") as out:
            out.write(data)

    def cue2sidecar(self, sidecar):
        if self.cue:
            with open(sidecar, "a") as out:
                out.write(f"{self.start},{self.cue}\n")

    def _extinf(self):
        if "#EXTINF" in self.tags:
            if isinstance(self.tags["#EXTINF"], str):
                self.tags["#EXTINF"] = self.tags["#EXTINF"].rsplit(",", 1)[0]
            self.duration = round(float(self.tags["#EXTINF"]), 6)

    def _scte35(self):
        if "#EXT-X-SCTE35" in self.tags:
            if "CUE" in self.tags["#EXT-X-SCTE35"]:
                self.cue = self.tags["#EXT-X-SCTE35"]["CUE"]
                if "CUE-OUT" in self.tags["#EXT-X-SCTE35"]:
                    if self.tags["#EXT-X-SCTE35"]["CUE-OUT"] == "YES":
                        self._do_cue()
                if "#EXT-X-CUE-OUT" in self.tags:
                    self._do_cue()
        if "#EXT-X-DATERANGE" in self.tags:
           # if "SCTE35-OUT" in self.tags["#EXT-X-DATERANGE"]:
             #   self.cue = self.tags["#EXT-X-DATERANGE"]["SCTE35-OUT"]
            self._do_cue()
            return
        if "#EXT-OATCLS-SCTE35" in self.tags:
            self.cue = self.tags["#EXT-OATCLS-SCTE35"]
            if isinstance(self.cue, dict):
                self.cue = self.cue.popitem()[0]
            self._do_cue()
            return
        if "#EXT-X-CUE-OUT-CONT" in self.tags:
            try:
                self.cue = self.tags["#EXT-X-CUE-OUT-CONT"]["SCTE35"]
                self._do_cue()
            except:
                pass

    def _do_cue(self):
        """
        _do_cue parses a SCTE-35 encoded string
        via the threefive.Cue class
        """
        if self.cue:
            try:
                tf = threefive.Cue(self.cue)
                tf.decode()
                if self.debug:
                    tf.show()
              #  self.cue_data = tf.get()
            except:
                pass

    def _chk_aes(self):
        if "#EXT-X-KEY" in self.tags:
            if "URI" in self.tags["#EXT-X-KEY"]:
                key_uri = self.tags["#EXT-X-KEY"]["URI"]
                if not key_uri.startswith("http"):
                    key_uri = self.base_uri + key_uri
                if "IV" in self.tags["#EXT-X-KEY"]:
                    iv = self.tags["#EXT-X-KEY"]["IV"]
                    decryptr = AESDecrypt(self.media, key_uri, iv)
                    self.tmp = decryptr.decrypt()
                    self.last_iv = iv
                    self.last_key_uri = key_uri
        else:
            if self.last_iv is not None:
                decryptr = AESDecrypt(self.media, self.last_key_uri, self.last_iv)
                self.tmp = decryptr.decrypt()

    def decode(self):
        self.tags = TagParser(self.lines).tags
        self._chk_aes()
        self._extinf()
        self._scte35()
        self._get_pts_start()
        if self.pts:
            self.start = self.pts
        if self.start:
            self.end = round(self.start + self.duration, 6)
        else:
            self.start = 0
        if self.debug:
            print("Media: ", self.media)
            print("Lines Read: ", self.lines)
            print("Vars : ", vars(self))
        # del self.lines

        return self.start

    def get_lines(self):
        return self.lines


class M3uFu:
    """
    M3u8 parser.
    """

    def __init__(self, shush=False):
        self.base_uri = ""
        self.sidecar = None
        self.next_expected = 0
        self.hls_time = 0.0
        self.desegment = False
        self.master = False
        self.reload = True
        self.m3u8 = None
        self.manifest = None
        self._start = None
        self.outfile = None
        self.media_list = deque()
        self.chunk = []
        self.segments = deque()
        self.headers = {}
        self.debug = False
        self.window_size = 10000
        self.shush = shush

    def _parse_args(self):
        """
        _parse_args parse command line args
        """
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "-i",
            "--input",
            default=None,
            help=""" Input source, like "/home/a/vid.ts"
                                    or "udp://@235.35.3.5:3535"
                                    or "https://futzu.com/xaa.ts"
                                    """,
        )
        parser.add_argument(
            "-s",
            "--sidecar",
            default=None,
            help="generate a SCTE35 sidecar file of pts, cue pairs and write them to this file ",
        )

        parser.add_argument(
            "-o",
            "--outfile",
            default=None,
            help=" download and reassemble segments and write to outfile. SCTE35 cues are written to sidecar.txt ",
        )

        parser.add_argument(
            "-v",
            "--version",
            action="store_const",
            default=False,
            const=True,
            help="Show version",
        )

        parser.add_argument(
            "-d",
            "--debug",
            action="store_const",
            default=False,
            const=True,
            help="Enable debug output.",
        )

        args = parser.parse_args()
        self._apply_args(args)

    @staticmethod
    def _args_version(args):
        if args.version:
            print(version())
            sys.exit()

    def _args_desegment(self, args):
        self.outfile = args.outfile

    def _args_input(self, args):
        if args.input:
            self.m3u8 = args.input

    def _args_debug(self, args):
        self.debug = args.debug

    def _args_sidecar(self, args):
        if args.sidecar:
            self.sidecar = args.sidecar
            with open(self.sidecar, "w+") as sidecar:  # touch sidecar
                pass

    def _apply_args(self, args):
        """
        _apply_args  uses command line args
        to set m3ufu instance vars
        """
        self._args_version(args)
        self._args_input(args)
        self._args_desegment(args)
        self._args_sidecar(args)
        self._args_debug(args)

    @staticmethod
    def _clean_line(line):
        if isinstance(line, bytes):
            line = line.decode(errors="ignore")
            line = line.replace("\n", "").replace("\r", "")
        return line

    def _is_master(self, line):
        playlist = False
        for this in ["STREAM-INF", "EXT-X-MEDIA"]:
            if this in line:
                self.master = True
                self.reload = False
                if "URI" in line:
                    playlist = line.split('URI="')[1].split('"')[0]
        return playlist

    def _set_times(self, segment):
        if not self._start:
            self._start = segment.start
        if not self._start:
            self._start = 0.0
        self._start += segment.duration
        self.next_expected = self._start + self.hls_time
        self.next_expected += round(segment.duration, 6)
        self.hls_time += segment.duration

    def _add_media(self, media):
        if media not in self.media_list:
            self.media_list.append(media)
            while len(self.media_list) > self.window_size:
                self.media_list.popleft()
            while len(self.segments) > self.window_size:
                self.segments.popleft()
            segment = HlsSegment(self.chunk, media, self._start, self.base_uri)
            if self.debug:
                segment.debug = True
            segment.decode()
            if not self.shush:
                print(json.dumps(segment.kv_clean(), indent=3))

            if self.outfile:
                segment.desegment(self.outfile)
            if self.sidecar:
                segment.cue2sidecar(self.sidecar)
            if segment.tmp:
                os.unlink(segment.tmp)
                del segment.tmp
            self.segments.append(segment)
            self._set_times(segment)

    def _do_media(self, line):
        playlist = self._is_master(line)
        if playlist:
            media = playlist
        else:
            media = line
            if self.base_uri not in line:
                if "http" not in line:
                    media = self.base_uri + media
        self._add_media(media)
        self.chunk = []

    def _parse_header(self, line):
        splitline = line.split(":", 1)
        if splitline[0] in HEADER_TAGS:
            val = ""
            tag = splitline[0]
            if len(splitline) > 1:
                val = splitline[1]
                try:
                    val = atoif(val)
                except:
                    pass
            self.headers[tag] = val
            return True
        return False

    def _parse_line(self, line):
        if not line:
            return False
        line = self._clean_line(line)
        if "ENDLIST" in line:
            self.reload = False
        if not self._parse_header(line):
            self._is_master(line)
            self.chunk.append(line)
            if (
                not line.startswith("#")
                or line.startswith("#EXT-X-I-FRAME-STREAM-INF")
                or line.startswith("EXT-X-MEDIA")
            ):
                if line:
                    self._do_media(line)
        return True

    def _get_window_size(self, m3u8_lines):
        if not self.window_size:
            self.window_size = len([line for line in m3u8_lines if b"#EXTINF:" in line])

    def decode(self):
        if self.desegment and os.path.exists(self.outfile):
            os.unlink(self.outfile)
        if self.m3u8:
            based = self.m3u8.rsplit("/", 1)
            if len(based) > 1:
                self.base_uri = f"{based[0]}/"
        while self.reload:
            with reader(self.m3u8) as self.manifest:
                m3u8_lines = self.manifest.readlines()
                self._get_window_size(m3u8_lines)
                for line in m3u8_lines:
                    if not self._parse_line(line):
                        break
                jason = {
                    "headers": self.headers,
                    "segments": [segment.kv_clean() for segment in self.segments],
                }
                if not self.shush:
                    print(json.dumps(jason, indent=2))
                if self.reload:
                    if "#EXT-X-TARGETDURATION" in self.headers:
                        time.sleep(self.headers["#EXT-X-TARGETDURATION"] / 2)


def cli():
    fu = M3uFu()
    fu._parse_args()
    fu.decode()


if __name__ == "__main__":
    cli()
