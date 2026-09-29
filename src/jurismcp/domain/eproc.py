"""Shared HTTP client for the eproc "Jurisprudência" portals (TNU, TRF4).

eproc, the electronic case-management system built by the TRF4 and also run
by the TNU/CJF, ships a public case-law search under
``externo_controlador.php?acao=jurisprudencia@jurisprudencia/...``. Each court
runs its own instance, but the form contract and the result markup are the
same, so this module holds one engine and each court is a thin subclass that
only sets its portal URL and its ``selOrigem[]`` codes (``tnu.py``,
``trf4.py``).

Contract measured live on 29/09/2026. Both bases render plain server-side
HTML: contrary to an earlier note, NO browser is needed.

* **Flow.** ``GET .../pesquisar`` returns the form and a ``PHPSESSID``
  cookie; ``POST .../listar_resultados`` returns the results page. The POST
  also worked without the GET on the measurement day; we still mirror the
  browser flow (one small request) in case the portal starts enforcing the
  session.
* **Form fields.** ``txtPesquisa`` (query), ``rdoCampo`` (``E`` = ementa,
  ``I`` = inteiro teor; the portal's own default is ``I``), ``selOrigem[]``
  (repeatable; TNU: 1=TNU; TRF4: 1=TRF4, 2=TRU4, 3=Turmas Recursais,
  4=Varas Federais), ``chkAgruparResultados=on``, ``selTamanhoPagina``
  (10/25/50/100), ``selOrdenacao`` (1 = most recent first, 2 = oldest; there
  is no relevance ranking) and ``hdnPaginaAtual`` (1-based page, honored by
  ``listar_resultados`` itself, so the AJAX pager is not needed).
* **Encoding.** Pages are ISO-8859-1 and the form declares no
  ``accept-charset``, so the POST body must be ISO-8859-1 percent-encoded (a
  UTF-8 body is echoed back as ``isenÃ§Ã£o``). Terms are ANDed; the portal
  documents the operators ``"..."``, ``e``, ``ou``, ``não``, ``prox`` and
  ``"prefix*"``.
* **Result blocks.** ``<div class="card mb-3 resultadoItem" id="resultado{ID}">``
  holding ``resLabel``/``resValue`` pairs: PROCESSO (number with an origin
  suffix, plus the class), UF, ÓRGÃO JULGADOR (TRF4 only), DATA DO
  JULGAMENTO, DATA DA PUBLICAÇÃO, RELATOR/RELATORA, RELATOR(A) PARA ACÓRDÃO,
  DECISÃO and EMENTA. DECISÃO is rendered twice, as a truncated ``limitado``
  copy and a hidden ``completo`` one; only ``completo`` is read.
* **Highlights.** Searched terms come wrapped in
  ``<B><FONT STYLE="background-color:#FFFF00">...</FONT></B>`` with NO space
  before the next punctuation mark. Swapping tags for a space (the usual
  shortcut) manufactures "RENDA ." artifacts, so tags are removed with the
  empty string and only whitespace is ever normalized, never words.
* **Citation.** ``data-citacao`` carries the portal's official citation but
  is LOSSY (the ementa's inner double quotes are dropped), so it is never the
  ementa source. Only its trailing reference
  ``(ORIGEM, CLASSE Nº, ÓRGÃO, Relator ..., julgado em ...)`` is used; its
  first item is how the portal attributes the decision (TNU, TRF4, TRU4 or
  "Turmas Recursais"). TRU4 decisions carry the same ``/TRF4`` process suffix
  as the court's own, so the suffix cannot tell them apart.
* **Inteiro teor.** Each block links to
  ``...download_inteiro_teor&id_jurisprudencia={ID}``: an HTML document that
  opens without a session (a stable ``full_text_url``), declared ISO-8859-1
  but decoded with a UTF-8 -> cp1252 -> latin-1 fallback (some are cp1252).
  Trap: with ``chkAgruparResultados=on`` decisions sharing an ementa are
  grouped, and a block's link may belong to ANOTHER process. The URL is built
  from the block's own id (links inside the block pointing elsewhere are
  logged), and a downloaded text is exposed only if it mentions the block's
  process number.
* **Missing ementa.** Turma Recursal acórdãos almost never have one (the
  EMENTA label is simply absent), so they only surface with ``rdoCampo=I``;
  for them the inteiro teor is downloaded into ``full_text``. Monocratic
  decisions have no ementa either, but their DECISÃO field already holds the
  whole decision (it matched the inteiro teor's body when measured), so
  nothing is downloaded for them.
* The OLD TNU base (``jurisprudencia.cjf.jus.br/tnu``, JSF/PrimeFaces,
  decisions up to jun/2017) did not answer plain POSTs and is out of scope.
"""

import asyncio
import html
import logging
import re
import unicodedata
from typing import TYPE_CHECKING, ClassVar, Final, Self, override
from urllib.parse import quote

import httpx
from pydantic import PrivateAttr

from jurismcp.domain.base import BaseLegalPrecedent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from patchright.async_api import Page

_LOGGER = logging.getLogger(__name__)

# The portal offers 10/25/50/100. 10 matches the other full-text sources
# (STJ, TJES): each result without an ementa costs one extra download, and
# 100 ementas per call overflow an MCP client's context budget.
_RESULTS_PER_PAGE = 10
_MAX_RETRIES = 2
_HTTP_TIMEOUT = 30.0
_FULL_TEXT_CONCURRENCY = 4
_FORM_ENCODING = "iso-8859-1"
_LATIN1_MAX = 0xFF
_MOST_RECENT_FIRST = "1"
_ACTION_PATH = "externo_controlador.php?acao=jurisprudencia@jurisprudencia/"

_HEADERS = {
    # Browser-like UA: house pattern for the HTTP sources.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

# User-facing search field -> ``rdoCampo`` code.
_SEARCH_FIELDS: Final[dict[str, str]] = {"ementa": "E", "inteiro_teor": "I"}

# Summary body when the portal holds no ementa for the decision.
_NO_EMENTA = "(Sem ementa na base do tribunal.)"

# Typographic quotes/dashes (which LLMs love) folded to the ASCII the portal's
# operators expect: a curly-quoted phrase must still be an exact-phrase search.
_TYPOGRAPHIC: Final = str.maketrans({
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2018": "'", "\u2019": "'", "\u201a": "'",
    "\u2013": "-", "\u2014": "-",
})

_RE_BLOCK_START = re.compile(
    r'<div class="card mb-3 resultadoItem" id="resultado(\d+)"'
)
_RE_LABEL = re.compile(r'<div class="resLabel">(.*?)</div>', re.DOTALL)
_RE_VALUE_COMPLETO = re.compile(
    r'<div class="resValue completo"[^>]*>(.*?)</div>', re.DOTALL
)
_RE_VALUE = re.compile(r'<div class="resValue[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_RE_TIPO = re.compile(
    r'<div class="resValueTipoJurisprudencia">(.*?)</div>', re.DOTALL
)
_RE_CLASSE = re.compile(r"<span>(.*?)</span>", re.DOTALL)
_RE_INTEIRO_TEOR_LINK = re.compile(
    r'<a\b[^>]*\bclass="[^"]*\binteiroTeor\b[^"]*"[^>]*\bdata-link="([^"]*)"'
)
_RE_ID_JURISPRUDENCIA = re.compile(r"[?&]id_jurisprudencia=(\d+)")
_RE_CITACAO = re.compile(r'\bdata-citacao="([^"]*)"')
# Trailing "(ORIGEM, CLASSE Nº, ÓRGÃO, Relator ..., julgado em ...)" reference.
_RE_CITACAO_REF = re.compile(r"(?P<ref>\((?P<origem>[^(),]+),[^()]*\))\s*$")
_RE_TOTAL = re.compile(r"([\d.]+)\s+documentos?\s+encontrad", re.IGNORECASE)
# "vencido o relator", "vencidos a relatora", "vencido parcialmente o relator".
_RE_WINNING_DISSENT = re.compile(
    r"\bvencid[oa]s?\s+(?:parcialmente\s+)?(?:o|a|o\(a\))\s+relator",
    re.IGNORECASE,
)

# HTML -> text. Structural tags become line breaks; every other tag (the
# search highlight included) is removed with the EMPTY string.
_RE_NON_CONTENT = re.compile(
    r"<(head|style|script)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE
)
_RE_MARKUP_DECL = re.compile(
    r"<\?.*?\?>|<!--.*?-->|<!DOCTYPE[^>]*>", re.DOTALL | re.IGNORECASE
)
_BLOCK_TAGS = "p|div|section|header|footer|article|table|tr|li|ul|ol|h[1-6]|blockquote"
_RE_BLOCK_BREAK = re.compile(
    rf"<\s*(?:br|hr)\b[^>]*>|</\s*(?:{_BLOCK_TAGS})\s*>", re.IGNORECASE
)
_RE_CELL_BREAK = re.compile(r"</\s*t[dh]\s*>", re.IGNORECASE)
_RE_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_RE_HSPACE = re.compile(r"[ \t\f\v\xa0]+")
_RE_SPACE_AROUND_NL = re.compile(r" *\n *")
_RE_BLANK_LINES = re.compile(r"\n{3,}")


def _fold(text: str) -> str:
    """Accent-free, upper-case, space-collapsed form used to compare labels."""
    decomposed = unicodedata.normalize("NFKD", text)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(bare.upper().split())


def _decode(raw: bytes) -> str:
    """Decode an eproc page: declared ISO-8859-1, cp1252 in practice at times.

    cp1252 goes before latin-1 so typographic quotes and dashes (0x80-0x9F)
    come out as themselves instead of C1 control characters; UTF-8 goes first
    in case a base ever switches. latin-1 never fails, so it closes the chain.
    """
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _html_to_text(fragment: str) -> str:
    """Render eproc HTML as plain text, normalizing whitespace and nothing else."""
    text = _RE_NON_CONTENT.sub("", fragment)
    text = _RE_MARKUP_DECL.sub("", text)
    text = _RE_BLOCK_BREAK.sub("\n", text)
    text = _RE_CELL_BREAK.sub(" ", text)
    text = _RE_TAG.sub("", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RE_HSPACE.sub(" ", text)
    text = _RE_SPACE_AROUND_NL.sub("\n", text)
    text = _RE_BLANK_LINES.sub("\n\n", text)
    return text.strip()


def _latin1_query(prompt: str) -> str:
    """Fold a query into what the ISO-8859-1 form can carry.

    NFC keeps accented letters as single latin-1 code points; typographic
    quotes/dashes become ASCII; anything still outside latin-1 keeps its
    latin-1 base letter when it has one ("ẽ" -> "e") and is dropped otherwise.
    """
    text = unicodedata.normalize("NFC", prompt).translate(_TYPOGRAPHIC)
    kept: list[str] = []
    for char in text:
        if ord(char) <= _LATIN1_MAX:
            kept.append(char)
            continue
        base = "".join(
            c
            for c in unicodedata.normalize("NFKD", char)
            if ord(c) <= _LATIN1_MAX and not unicodedata.combining(c)
        )
        kept.append(base or " ")
    return " ".join("".join(kept).split())


def _build_form_body(
    query: str, campo: str, origin_codes: "Sequence[str]", page: int
) -> bytes:
    """URL-encoded ``listar_resultados`` body, values in ISO-8859-1."""
    pairs = [("txtPesquisa", query), ("rdoCampo", campo)]
    pairs += [("selOrigem[]", code) for code in origin_codes]
    pairs += [
        ("chkAgruparResultados", "on"),
        ("selTamanhoPagina", str(_RESULTS_PER_PAGE)),
        ("selOrdenacao", _MOST_RECENT_FIRST),
        ("hdnPaginaAtual", str(page)),
    ]
    return "&".join(
        f"{quote(key, safe='')}={quote(value, safe='', encoding=_FORM_ENCODING)}"
        for key, value in pairs
    ).encode("ascii")


def _mentions_process(document: str, numero: str) -> bool:
    """Whether ``document`` cites the process ``numero`` in any usual format.

    Matches the digits in order with optional separators, which covers
    ``5000152-42.2023.4.04.7102``, the bare ``50001524220234047102`` of the
    ``data-numero_processo`` attributes and the legacy ``...2023.404.7102``.
    """
    digits = re.sub(r"\D", "", numero)
    if not digits:
        return False
    pattern = r"(?<!\d)" + r"[.\-/ ]?".join(digits) + r"(?!\d)"
    return re.search(pattern, document) is not None


def _verified_full_text(document: str, numero: str | None) -> str | None:
    """Plain text of a downloaded inteiro teor, or None if not about ``numero``."""
    if not numero or not _mentions_process(document, numero):
        return None
    return _html_to_text(document) or None


def _block_fields(block: str) -> dict[str, str]:
    """Map each folded ``resLabel`` to its raw HTML, up to the next label."""
    labels = list(_RE_LABEL.finditer(block))
    fields: dict[str, str] = {}
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(block)
        fields.setdefault(_fold(label.group(1)), block[label.end() : end])
    return fields


def _field_text(segment: str | None) -> str:
    """Text of a label's value, preferring the ``completo`` rendering."""
    if not segment:
        return ""
    match = _RE_VALUE_COMPLETO.search(segment) or _RE_VALUE.search(segment)
    return _html_to_text(match.group(1)) if match else ""


def _parse_processo(segment: str) -> tuple[str, str, str]:
    """Split the PROCESSO cell into (number as shown, bare number, class).

    The number is shown with the origin suffix eproc appends
    (``0018320-54.2019.4.01.3400/TNU``); the bare number drops it, since a
    suffix such as ``/TRF4`` would pollute a digit-based comparison.
    """
    classe_match = _RE_CLASSE.search(segment)
    classe = _html_to_text(classe_match.group(1)) if classe_match else ""
    head = segment[: classe_match.start()] if classe_match else segment
    shown = next((line for line in _html_to_text(head).splitlines() if line), "")
    bare, slash, _suffix = shown.rpartition("/")
    return shown, bare if slash else shown, classe


def _citation_reference(block: str) -> tuple[str, str]:
    """(official citation reference, origin) from ``data-citacao``; '' if absent."""
    match = _RE_CITACAO.search(block)
    if not match:
        return "", ""
    reference = _RE_CITACAO_REF.search(html.unescape(match.group(1)))
    if not reference:
        return "", ""
    return " ".join(reference.group("ref").split()), reference.group("origem").strip()


class EprocLegalPrecedent(BaseLegalPrecedent):
    """Decision from an eproc "Jurisprudência" portal; subclassed per court."""

    requires_browser: ClassVar[bool] = False  # server-rendered HTML over plain HTTP

    portal_url: ClassVar[str] = ""
    """Portal root ending in ``/``; set by each court subclass."""

    source: ClassVar[str] = "EPROC"
    """Short court name used in logs and error messages."""

    origin_codes: ClassVar[dict[str, str]] = {}
    """User-facing origin key -> ``selOrigem[]`` code; searches default to all."""

    _numero_processo: str | None = PrivateAttr(default=None)
    _needs_full_text: bool = PrivateAttr(default=False)

    @classmethod
    def _parse_results(cls, page_html: str) -> list[Self]:
        """Extract the decisions of one results page (pure: no I/O)."""
        starts = list(_RE_BLOCK_START.finditer(page_html))
        if not starts:
            total = _RE_TOTAL.search(page_html)
            if total:
                _LOGGER.info(
                    "%s returned no results on this page (%s in total)",
                    cls.source,
                    total.group(1),
                )
            else:
                _LOGGER.warning(
                    "%s: no result blocks nor counter (template may have changed)",
                    cls.source,
                )
            return []

        results: list[Self] = []
        for index, start in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else None
            precedent = cls._parse_block(start.group(1), page_html[start.start() : end])
            if precedent is not None:
                results.append(precedent)

        _LOGGER.info("Parsed %d decision(s) from %s", len(results), cls.source)
        return results

    @classmethod
    def _parse_block(cls, block_id: str, block: str) -> Self | None:
        """Build one precedent from a result block; None when it is malformed."""
        fields = _block_fields(block)
        shown, numero, classe = _parse_processo(fields.get("PROCESSO", ""))
        if not shown:
            _LOGGER.warning(
                "%s: discarding block %s without a process number", cls.source, block_id
            )
            return None

        tipo_match = _RE_TIPO.search(block)
        tipo = _html_to_text(tipo_match.group(1)) if tipo_match else ""
        relator = _field_text(fields.get("RELATOR") or fields.get("RELATORA"))
        relator_acordao = _field_text(
            fields.get("RELATOR PARA ACORDAO") or fields.get("RELATORA PARA ACORDAO")
        )
        ementa = _field_text(fields.get("EMENTA"))
        decisao = _field_text(fields.get("DECISAO"))
        reference, origem = _citation_reference(block)

        header = [f"Processo: {shown}"]
        for label, value in (
            ("Classe", classe),
            ("Tipo", tipo),
            ("Órgão julgador", _field_text(fields.get("ORGAO JULGADOR"))),
            ("UF", _field_text(fields.get("UF"))),
            ("Relator(a)", relator),
            ("Relator(a) p/ acórdão", relator_acordao),
            ("Julgamento", _field_text(fields.get("DATA DO JULGAMENTO"))),
            ("Publicação", _field_text(fields.get("DATA DA PUBLICACAO"))),
        ):
            if value:
                header.append(f"{label}: {value}")

        lines = ["[" + " | ".join(header) + "]", ementa or _NO_EMENTA]
        if decisao:
            lines.append(f"Decisão: {decisao}")
        if reference:
            lines.append(f"Citação: {reference}")

        # A distinct "relator para acórdão" means the original relator did not
        # write the acórdão; it is a winning dissent only when DECISÃO says the
        # relator was defeated (a successor may also sign the acórdão).
        dissent = bool(relator and relator_acordao) and _fold(relator) != _fold(
            relator_acordao
        )
        full_text_url = cls._full_text_url(block_id, block)
        precedent = cls(
            summary="\n".join(lines),
            full_text_url=full_text_url,
            relator_original=relator if dissent else None,
            divergencia_vencedora=dissent
            and _RE_WINNING_DISSENT.search(decisao) is not None,
            court=(origem or None) if len(cls.origin_codes) > 1 else None,
        )
        precedent._numero_processo = numero
        # Monocratic decisions carry their whole text in DECISÃO; an acórdão
        # there only states the outcome, its reasoning lives in the inteiro teor.
        decisao_is_whole_text = bool(decisao and tipo) and not _fold(tipo).startswith(
            "ACORDAO"
        )
        precedent._needs_full_text = (
            not ementa and not decisao_is_whole_text and full_text_url is not None
        )
        return precedent

    @classmethod
    def _full_text_url(cls, block_id: str, block: str) -> str | None:
        """Inteiro-teor URL of the block's OWN document (see the grouping trap)."""
        link_ids = [
            match.group(1)
            for link in _RE_INTEIRO_TEOR_LINK.finditer(block)
            if (match := _RE_ID_JURISPRUDENCIA.search(html.unescape(link.group(1))))
        ]
        if not link_ids:
            return None
        if block_id not in link_ids:
            _LOGGER.warning(
                "%s: block %s links the inteiro teor of %s; using the block's own id",
                cls.source,
                block_id,
                ", ".join(link_ids),
            )
        return (
            f"{cls.portal_url}{_ACTION_PATH}download_inteiro_teor"
            f"&id_jurisprudencia={block_id}"
        )

    @classmethod
    def _origin_codes_for(cls, origens: "Sequence[str] | None") -> list[str]:
        """``selOrigem[]`` codes of the requested origins (every one when empty)."""
        if isinstance(origens, str):
            origens = [origens]
        if not origens:
            return list(cls.origin_codes.values())
        codes: list[str] = []
        for origem in origens:
            code = cls.origin_codes.get(origem.strip().upper())
            if code is None:
                raise RuntimeError(
                    f"{cls.source}: origem '{origem}' desconhecida. Use uma de: "
                    + ", ".join(cls.origin_codes)
                )
            if code not in codes:
                codes.append(code)
        return codes

    @classmethod
    async def _download_full_text(
        cls,
        client: httpx.AsyncClient,
        result: Self,
        semaphore: asyncio.Semaphore,
    ) -> str | None:
        """Fetch and verify one inteiro teor; None (logged) when unusable."""
        url = result.full_text_url
        if url is None:
            return None
        async with semaphore:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                _LOGGER.warning(
                    "%s: inteiro teor of %s unavailable: %s",
                    cls.source,
                    result._numero_processo,
                    exc,
                )
                return None
        text = _verified_full_text(_decode(response.content), result._numero_processo)
        if text is None:
            _LOGGER.warning(
                "%s: inteiro teor at %s does not mention process %s; discarded",
                cls.source,
                url,
                result._numero_processo,
            )
        return text

    @classmethod
    async def _attach_full_texts(
        cls, client: httpx.AsyncClient, results: "Sequence[Self]"
    ) -> None:
        """Download the inteiro teor of the results that have no ementa."""
        pending = [result for result in results if result._needs_full_text]
        if not pending:
            return
        semaphore = asyncio.Semaphore(_FULL_TEXT_CONCURRENCY)
        texts = await asyncio.gather(
            *(cls._download_full_text(client, result, semaphore) for result in pending)
        )
        for result, text in zip(pending, texts, strict=True):
            result.full_text = text

    @override
    @classmethod
    async def research(
        cls,
        browser: "Page",  # interface compatibility; not used (HTTP, no browser)
        *,
        summary_search_prompt: str,
        desired_page: int = 1,
        campo: str = "ementa",
        origens: "Sequence[str] | None" = None,
    ) -> list[Self]:
        """Search the portal through its HTML form (session GET + results POST).

        :param campo: where the terms are searched, ``ementa`` (default) or
            ``inteiro_teor`` (``rdoCampo`` E/I).
        :param origens: keys of ``origin_codes`` (e.g. ``["TRU4", "TR"]``);
            empty means every origin the court exposes.
        """
        query = _latin1_query(summary_search_prompt)
        if not query:
            raise RuntimeError(f"{cls.source}: informe os termos de busca.")
        campo_code = _SEARCH_FIELDS.get(campo.strip().lower())
        if campo_code is None:
            raise RuntimeError(
                f"{cls.source}: campo '{campo}' inválido. Use: "
                + " ou ".join(_SEARCH_FIELDS)
            )
        origin_codes = cls._origin_codes_for(origens)
        page = max(desired_page, 1)
        body = _build_form_body(query, campo_code, origin_codes, page)
        form_url = f"{cls.portal_url}{_ACTION_PATH}pesquisar"
        results_url = f"{cls.portal_url}{_ACTION_PATH}listar_resultados"

        _LOGGER.info(
            "%s research: q=%r campo=%s origens=%s page=%d",
            cls.source,
            query,
            campo_code,
            origin_codes,
            page,
        )

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=_HTTP_TIMEOUT,
                    follow_redirects=True,
                    headers=_HEADERS,
                ) as client:
                    # Opens the PHP session the way a browser would (module doc).
                    (await client.get(form_url)).raise_for_status()
                    response = await client.post(
                        results_url,
                        content=body,
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "Referer": form_url,
                        },
                    )
                    response.raise_for_status()
                    results = cls._parse_results(_decode(response.content))
                    await cls._attach_full_texts(client, results)
                    return results

            except httpx.HTTPError as exc:
                last_error = exc
                _LOGGER.warning(
                    "%s attempt %d/%d failed: %s",
                    cls.source,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )

        raise RuntimeError(
            f"{cls.source} research failed after {_MAX_RETRIES} attempts"
        ) from last_error
