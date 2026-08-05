import io
import pathlib
import random
import zlib

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
)

from jobdeck import config, db, migrations, netsafe

_REAL_RESOLVER = netsafe._system_resolver


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """No test may hit real DNS: the netsafe resolver seam answers a public
    address for every hostname. SSRF tests override it with their own fake."""
    async def fake_resolver(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(netsafe, "_system_resolver", fake_resolver)


@pytest.fixture()
def real_resolver(monkeypatch):
    """Restore the REAL resolver seam for the few tests that must prove the
    production implementation's own behavior. Only safe for hosts getaddrinfo
    rejects while IDNA-encoding, i.e. before any network I/O."""
    monkeypatch.setattr(netsafe, "_system_resolver", _REAL_RESOLVER)
    return _REAL_RESOLVER


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Redirect the whole data directory into a temp folder."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobdeck.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "profile.md")
    monkeypatch.setattr(config, "TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "client_secret.json")
    config.ensure_data_dirs()
    return tmp_path


@pytest.fixture()
def con(data_dir):
    """Migrated test database, WAL from the start like production.

    Must go through db.connect(): a plain sqlite3.connect would leave the
    file in delete journal mode, and the service's first concurrent
    connections would then race on the delete→WAL conversion — an
    exclusive-lock operation that fails fast ("database is locked")."""
    con = db.connect()
    migrations.migrate(con)
    yield con
    con.close()


def _noisy(width: int, height: int) -> Image.Image:
    """Incompressible content — a smooth image would shrink under Flate and
    make a 'compression saved bytes' assertion pass for the wrong reason."""
    rng = random.Random(7)
    image = Image.new("RGB", (width, height))
    image.putdata([(rng.randrange(256),) * 3 for _ in range(width * height)])
    return image


def _line_art(width: int, height: int) -> Image.Image:
    """Flat colour and hard edges — what Flate stores in a few KB and JPEG
    stores badly. A scanned black-and-white certificate looks like this."""
    image = Image.new("RGB", (width, height), "white")
    drawing = ImageDraw.Draw(image)
    for y in range(0, height, 40):
        drawing.line((0, y, width, y), fill="black", width=3)
    return image


def _image_xobject(writer: PdfWriter, image: Image.Image, *, lossless: bool,
                   smask: bool = False, bits: int = 8,
                   colourspace=None) -> EncodedStreamObject:
    family = str(colourspace[0] if isinstance(colourspace, list) else colourspace)
    if bits != 8:
        # 16-bit samples: two bytes per pixel, which pypdf hands back as a
        # mangled 8-bit "L"
        data = zlib.compress(image.convert("L").tobytes() * 2, 6)
        filt = NameObject("/FlateDecode")
    elif family == "/DeviceCMYK":
        data = zlib.compress(image.convert("CMYK").tobytes(), 6)
        filt = NameObject("/FlateDecode")
    elif family == "/Indexed":
        data = zlib.compress(image.convert("L").tobytes(), 6)  # one index/pixel
        filt = NameObject("/FlateDecode")
    elif lossless:
        data = zlib.compress(image.tobytes(), 9)
        filt = NameObject("/FlateDecode")
    else:
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=95)
        data, filt = buf.getvalue(), NameObject("/DCTDecode")
    stream = EncodedStreamObject()
    stream.update({
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(image.width),
        NameObject("/Height"): NumberObject(image.height),
        NameObject("/ColorSpace"): colourspace or NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(bits),
        NameObject("/Filter"): filt,
    })
    stream._data = data
    if smask:
        mask = EncodedStreamObject()
        mask.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(image.width),
            NameObject("/Height"): NumberObject(image.height),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Filter"): NameObject("/FlateDecode"),
        })
        mask._data = zlib.compress(b"\xff" * (image.width * image.height), 9)
        stream[NameObject("/SMask")] = writer._add_object(mask)
    return stream


def _pdf_with_images(path: pathlib.Path, specs: list[dict], *,
                     dpi: float = 300.0, pages: int = 1) -> pathlib.Path:
    """Pages carrying each spec's image, sized so the FIRST image sits at
    exactly `dpi` when it covers the page. With `pages` > 1 every page draws
    the very same image objects, as the CV photo does on Deckblatt and
    Lebenslauf."""
    writer = PdfWriter()
    first = specs[0]["image"]
    width, height = first.width / dpi * 72, first.height / dpi * 72
    refs, draw = [], []
    for index, spec in enumerate(specs):
        stream = _image_xobject(writer, spec["image"],
                                lossless=spec.get("lossless", True),
                                smask=spec.get("smask", False),
                                bits=spec.get("bits", 8),
                                colourspace=spec.get("colourspace"))
        if spec.get("image_mask"):
            stream[NameObject("/ImageMask")] = BooleanObject(True)
        if spec.get("decode"):
            stream[NameObject("/Decode")] = ArrayObject(
                [NumberObject(1), NumberObject(0)] * 3
            )
        if spec.get("colour_key_mask"):
            stream[NameObject("/Mask")] = ArrayObject(
                [NumberObject(0), NumberObject(10)] * 3
            )
        refs.append((f"/Im{index}", writer._add_object(stream)))
        draw.append(f"q {width} 0 0 {height} 0 0 cm /Im{index} Do Q")
    content = DecodedStreamObject()
    content.set_data("\n".join(draw).encode("ascii"))
    content_ref = writer._add_object(content)
    for _ in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        resources = page.setdefault(NameObject("/Resources"), DictionaryObject())
        xobjects = resources.setdefault(NameObject("/XObject"), DictionaryObject())
        for name, ref in refs:
            xobjects[NameObject(name)] = ref
        page[NameObject("/Contents")] = content_ref
    with path.open("wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture()
def image_pdf():
    """Factory for PDFs carrying real image XObjects — the only way to
    exercise the Mappe's compression, which is decided per image from the
    object dictionary."""
    return _pdf_with_images


@pytest.fixture()
def noisy_image():
    return _noisy


@pytest.fixture()
def line_art_image():
    return _line_art
