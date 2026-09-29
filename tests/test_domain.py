import asyncio

import pytest

from jurismcp.domain.base import BaseLegalPrecedent
from jurismcp.domain.bnp import BnpLegalPrecedent
from jurismcp.domain.lexml import LexmlLegalPrecedent
from jurismcp.domain.stf import StfLegalPrecedent
from jurismcp.domain.stj import StjLegalPrecedent
from jurismcp.domain.tjes import TjesLegalPrecedent
from jurismcp.domain.tnu import TnuLegalPrecedent
from jurismcp.domain.trf4 import Trf4LegalPrecedent
from jurismcp.domain.tst import TstLegalPrecedent
from jurismcp.utils import browser_factory


@pytest.mark.parametrize(
    ("summary", "should_return_results"),
    [
        pytest.param(
            "asdjnaskjdnaajhsbajkhsdjkabsndk12931092381902098",  # Bogus criteria
            False,
            id="should_not_return_results",
        ),
        pytest.param(
            "fraude execução",  # Criteria known to return results.
            True,
            id="should_return_results",
        ),
    ],
)
@pytest.mark.parametrize(
    "class_", [StjLegalPrecedent, TstLegalPrecedent, StfLegalPrecedent]
)
async def test_research_legal_precedents(
    summary: str,
    should_return_results: bool,
    class_: type[BaseLegalPrecedent],
) -> None:
    """Test the research method of a legal precedent class.

    :param summary: The summary to search for.
    :param should_return_results: Whether the research should return results."""

    async with (
        asyncio.timeout(30),
        browser_factory() as browser,
    ):
        page = await browser.new_page()

        for desired_results_page in range(1, 3):
            precedents = await class_.research(
                page,
                summary_search_prompt=summary,
                desired_page=desired_results_page,
            )

            assert should_return_results == bool(precedents)
            if not should_return_results:
                return

            assert all(isinstance(precedent, class_) for precedent in precedents)


@pytest.mark.parametrize(
    ("summary", "should_return_results"),
    [
        pytest.param(
            "asdjnaskjdnaajhsbajkhsdjkabsndk12931092381902098",  # Bogus criteria
            False,
            id="should_not_return_results",
        ),
        pytest.param(
            "fraude execução",  # Criteria known to return results.
            True,
            id="should_return_results",
        ),
    ],
)
@pytest.mark.parametrize(
    "class_",
    [
        StjLegalPrecedent,
        TjesLegalPrecedent,
        LexmlLegalPrecedent,
        BnpLegalPrecedent,
        Trf4LegalPrecedent,
    ],
)
async def test_research_http_legal_precedents(
    summary: str,
    should_return_results: bool,
    class_: type[BaseLegalPrecedent],
) -> None:
    """Integration test for HTTP-based courts (no browser).

    STJ, TJES and LexML set ``requires_browser=False`` and scrape an
    HTTP endpoint directly, so we pass ``browser=None`` and skip launching
    Chromium. Mirrors the browser test's bogus/known-good query pair.

    :param summary: The summary to search for.
    :param should_return_results: Whether the research should return results."""
    assert class_.requires_browser is False

    async with asyncio.timeout(30):
        for desired_results_page in range(1, 3):
            precedents = await class_.research(
                None,  # pyright: ignore[reportArgumentType] — browser not used
                summary_search_prompt=summary,
                desired_page=desired_results_page,
            )

            assert should_return_results == bool(precedents)
            if not should_return_results:
                return

            assert all(isinstance(precedent, class_) for precedent in precedents)


@pytest.mark.parametrize(
    ("summary", "should_return_results"),
    [
        pytest.param(
            "asdjnaskjdnaajhsbajkhsdjkabsndk12931092381902098",  # Bogus criteria
            False,
            id="should_not_return_results",
        ),
        pytest.param(
            # "fraude execução" has a single TNU ementa, and the shared test
            # above needs two pages; the TNU's docket is mostly social security.
            "aposentadoria especial",
            True,
            id="should_return_results",
        ),
    ],
)
async def test_research_tnu_legal_precedents(
    summary: str,
    should_return_results: bool,
) -> None:
    """Integration test for the TNU (HTTP, no browser), two pages deep."""
    async with asyncio.timeout(30):
        for desired_results_page in range(1, 3):
            precedents = await TnuLegalPrecedent.research(
                None,  # pyright: ignore[reportArgumentType] — browser not used
                summary_search_prompt=summary,
                desired_page=desired_results_page,
            )

            assert should_return_results == bool(precedents)
            if not should_return_results:
                return

            assert all(isinstance(p, TnuLegalPrecedent) for p in precedents)


async def test_tnu_smoke_brings_the_puil_of_30_06_2026() -> None:
    """Acceptance smoke (29/09/2026): the ementa search for "isenção imposto
    renda neoplasia maligna" must bring PUIL 0018320-54.2019.4.01.3400."""
    async with asyncio.timeout(30):
        precedents = await TnuLegalPrecedent.research(
            None,  # pyright: ignore[reportArgumentType] — browser not used
            summary_search_prompt="isenção imposto renda neoplasia maligna",
        )

    assert any(
        p.summary.startswith("[Processo: 0018320-54.2019.4.01.3400/TNU")
        and "Julgamento: 30/06/2026" in p.summary
        for p in precedents
    )


async def test_trf4_turmas_recursais_come_with_verified_full_text() -> None:
    """Turma Recursal acórdãos have no ementa: they surface in the inteiro-teor
    search and carry the downloaded text, checked against the process number."""
    async with asyncio.timeout(60):
        precedents = await Trf4LegalPrecedent.research(
            None,  # pyright: ignore[reportArgumentType] — browser not used
            summary_search_prompt="isenção imposto renda neoplasia maligna",
            campo="inteiro_teor",
            origens=["TR"],
        )

    assert precedents
    assert all(p.court == "Turmas Recursais" for p in precedents)
    with_full_text = [p for p in precedents if p.full_text]
    assert with_full_text
    for precedent in with_full_text:
        numero = precedent.summary.removeprefix("[Processo: ").split("/")[0]
        assert precedent.full_text is not None
        assert numero in precedent.full_text
