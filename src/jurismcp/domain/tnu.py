"""TNU (Turma Nacional de Uniformização dos JEFs) case-law source.

Thin subclass of the shared eproc engine: see ``eproc.py`` for the portal
contract and every integration quirk. The TNU runs its own eproc instance at
``eproctnu-jur.cjf.jus.br``, whose only origin is ``selOrigem[]=1`` (TNU).

Out of scope: the OLD TNU base (``jurisprudencia.cjf.jus.br/tnu``, a
JSF/PrimeFaces app with decisions up to jun/2017), which did not answer plain
POSTs when measured (29/09/2026) and would need a browser.
"""

from typing import ClassVar

from jurismcp.domain.eproc import EprocLegalPrecedent


class TnuLegalPrecedent(EprocLegalPrecedent):
    """Model for a decision of the Turma Nacional de Uniformização (TNU)."""

    portal_url: ClassVar[str] = "https://eproctnu-jur.cjf.jus.br/eproc/"
    source: ClassVar[str] = "TNU"
    origin_codes: ClassVar[dict[str, str]] = {"TNU": "1"}
