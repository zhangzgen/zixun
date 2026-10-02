from unittest.mock import patch
from technical_reports.adapters_west import primary_original, _team_index, _team_publication_date, _anthropic_team_sites


def test_explicit_primary_skips_cited_pdf_and_preserves_named_supplement():
    page = '''<article><h1>Our new method</h1>
      <p>Prior work used <a href="https://example.org/old.pdf">a previous paper</a>.</p>
      <a href="/files/current.pdf">Read the paper</a>
      <p>Supplementary information is available <a href="/files/supplement.pdf">here</a>.</p>
      <h2>References</h2><p><a href="https://example.org/reference.pdf">Paper</a></p>
      </article>'''
    resolved = primary_original('https://www.anthropic.com/research/method', page)
    assert resolved['report_url'] == 'https://www.anthropic.com/files/current.pdf'
    assert resolved['supplementary_urls'] == ['https://www.anthropic.com/files/supplement.pdf']


def test_deployment_safety_pdf_button_ignores_navigation_and_reference_reports():
    page = '''<nav><a href="/old.pdf">View PDF</a></nav>
      <main><h1>Current system card</h1><a href="/model/model.pdf">View PDF↗</a>
      <p>A comparison is in the <a href="/other.pdf">other technical report</a>.</p></main>'''
    resolved = primary_original('https://deploymentsafety.openai.com/model', page)
    assert resolved['report_url'] == 'https://deploymentsafety.openai.com/model/model.pdf'


def test_team_paper_button_normalizes_arxiv_original_without_following_code():
    page = '''<h1>Alignment experiment</h1><p>📄 <a href="https://arxiv.org/abs/2605.02087v2">Paper</a>,
      <a href="https://github.com/example/project">💻 Code</a></p>'''
    assert primary_original('https://alignment.anthropic.com/2026/example/', page)['report_url'] == 'https://arxiv.org/pdf/2605.02087v2'


def test_citation_only_article_does_not_claim_a_complete_paper():
    page = '''<h1>Research overview</h1><p>See this older <a href="/old.pdf">paper</a>.</p>
      <h2>References</h2><a href="/reference.pdf">Read the paper</a>
      <p>Supplementary material is available <a href="/not-ours.pdf">here</a>.</p>'''
    resolved = primary_original('https://www.anthropic.com/research/overview', page)
    assert 'report_url' not in resolved
    assert resolved['supplementary_urls'] == []


def test_team_date_uses_explicit_day_not_editorial_month_or_experiment_date():
    index = '''<div class="date">May 2026</div>
      <a href="2026/may-update/index.html" data-date="2026-06-01" class="note">
      <h3>Circuits Updates — May 2026</h3><div class="description">Methods update.</div></a>'''
    rows = list(_team_index(index, 'https://transformer-circuits.pub/'))
    assert rows[0]['published_at'] == '2026-06-01'
    assert rows[0]['date_precision'] == 'day'
    month_only = '''<div class="section-authors"><div>Author<div>August 2026</div></div></div>
      <p>Model released on June 8, 2026.</p>'''
    assert _team_publication_date(month_only) is None


def test_publication_byline_includes_nested_divs():
    page = '''<div class="section-authors"><div>Authors<div>April 24, 2026</div></div>
      <div>Affiliations</div></div><p>Experiment ran March 1, 2026.</p>'''
    assert _team_publication_date(page) == '2026-04-24'


def test_html_original_can_have_a_separate_explicit_appendix():
    page = '''<h1>Full research note</h1><p>Complete methodology and results.</p>
      <h2>Appendix</h2><p>Available <a href="/appendix.pdf">here</a>.</p>
      <h2>References</h2><p><a href="/prior.pdf">Download PDF</a></p>'''
    resolved = primary_original('https://alignment.anthropic.com/2026/note/', page)
    assert 'report_url' not in resolved
    assert resolved['supplementary_urls'] == ['https://alignment.anthropic.com/appendix.pdf']


def test_month_only_dates_cannot_prove_a_partial_boundary_inclusion():
    index = '''<div class="date">April 2026</div><a href="2026/new-note/" class="note">
      <h3>New alignment study</h3><div class="description">Research methods.</div></a>'''
    def fetch(url):
        if url == 'https://alignment.anthropic.com/':
            return index
        if url == 'https://transformer-circuits.pub/':
            return ''
        return '<h1>New alignment study</h1><p>April 2026</p>'
    with patch('technical_reports.adapters_west._fetch', side_effect=fetch):
        result = _anthropic_team_sites('2026-04-02', '2026-10-02')
    assert result['items'] == []
    assert result['coverage']['complete'] is False
    assert 'exact publication day unavailable' in result['errors'][0]['error']


def test_another_reports_appendix_is_a_citation_not_an_attachment():
    page = '''<h1>Current research note</h1><p>Prior methods are described in the
      <a href="https://transformer-circuits.pub/2025/attribution-graphs/methods.html">Appendix</a>
      of an earlier paper.</p><p>Compare the results in
      <a href="https://www-cdn.anthropic.com/prior.pdf">Risk Report Appendix 2.25</a>.</p>'''
    resolved = primary_original('https://transformer-circuits.pub/2026/new-note/index.html', page)
    assert resolved['supplementary_urls'] == []
    assert 'report_url' not in resolved


def test_versioned_download_takes_precedence_over_stale_citation_metadata():
    page = '''<meta name="citation_title" content="A benchmark">
      <meta name="citation_pdf_url" content="https://www.biorxiv.org/content/old.full.pdf">
      <h1>A benchmark</h1><a href="/content/currentv2.full.pdf">Download PDF</a>'''
    resolved = primary_original('https://www.biorxiv.org/content/currentv2', page)
    assert resolved['report_url'] == 'https://www.biorxiv.org/content/currentv2.full.pdf'


def test_anthropic_read_in_pdf_button_selects_complete_report():
    page = '<h1>Agentic coding research</h1><a href="https://cdn.sanity.io/report.pdf">Read in PDF</a>'
    resolved = primary_original('https://www.anthropic.com/research/coding', page)
    assert resolved['report_url'] == 'https://cdn.sanity.io/report.pdf'
