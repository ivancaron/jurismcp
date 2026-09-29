"""TRF4 case-law source: the TRF4, the TRU4 and the 4th Region's Turmas Recursais.

Thin subclass of the shared eproc engine: see ``eproc.py`` for the portal
contract and every integration quirk. The TRF4 base
(``jurisprudencia.trf4.jus.br/eproc2trf4``) federates four origins in
``selOrigem[]``: 1=TRF4, 2=TRU4 (Turma Regional de Uniformização),
3=Turmas Recursais (the JEFs of RS, SC and PR) and 4=Varas Federais. Only the
first three are exposed; first-instance sentences are out of scope.

Since one search mixes origins, ``court`` carries the portal's own attribution
("TRF4", "TRU4" or "Turmas Recursais", from the official citation): the
process suffix cannot tell them apart (TRU4 decisions end in ``/TRF4`` like
the court's own; Turmas Recursais end in the state, e.g. ``/RS``).
"""

from typing import ClassVar

from jurismcp.domain.eproc import EprocLegalPrecedent


class Trf4LegalPrecedent(EprocLegalPrecedent):
    """Model for a decision of the TRF4, the TRU4 or a 4th Region Turma Recursal."""

    portal_url: ClassVar[str] = "https://jurisprudencia.trf4.jus.br/eproc2trf4/"
    source: ClassVar[str] = "TRF4"
    origin_codes: ClassVar[dict[str, str]] = {"TRF4": "1", "TRU4": "2", "TR": "3"}
