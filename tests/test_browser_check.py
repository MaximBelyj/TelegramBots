import unittest
from dmr_bot.browser_check import inspect_document, summarize_navigation


class BrowserDiagnosticTests(unittest.TestCase):
    def test_rendered_results_are_structural_evidence(self):
        html = '<a href="/product/radio-123/">Радиостанция DMR</a>'
        row = inspect_document('ozon', html, 'https://www.ozon.ru/search/?text=DMR')
        self.assertEqual(row['classification'], 'results')
        self.assertEqual(row['candidate_count'], 1)

    def test_categories_cannot_be_reported_as_search_success(self):
        html = '<a href="/product/radio-123/">Радиостанция DMR</a>'
        self.assertEqual(inspect_document('ozon', html, 'https://www.ozon.ru/category/audio/')['classification'], 'search_context_lost')

    def test_login_challenge_and_response_denial(self):
        for html, expected in [('<title>Авторизация</title>', 'login'), ('<title>CAPTCHA</title>', 'challenge')]:
            self.assertEqual(inspect_document('wb', html, 'https://www.wildberries.ru/catalog/0/search.aspx?search=DMR')['classification'], expected)
        statuses = [{'status': 498, 'host': 'www.wildberries.ru', 'path': '/catalog/0/search.aspx'}]
        row = summarize_navigation('wb', '<title>Error</title>', 'https://www.wildberries.ru/catalog/0/search.aspx?search=DMR', statuses)
        self.assertEqual(row['classification'], 'http_498')

    def test_forum_form_metadata_excludes_values_and_query_tokens(self):
        html = '''<form action="/forum/search.php?sid=DO_NOT_SHARE" method="post">
        <input name="search_keywords" value="PRIVATE_QUERY"><input name="csrf" value="PRIVATE_TOKEN"></form>'''
        row = inspect_document('radiokot', html, 'https://radiokot.ru/forum/search.php?keywords=LM358')
        self.assertEqual(row['classification'], 'search_form_instead_of_results')
        self.assertEqual(inspect_document('radiokot', html, 'https://radiokot.ru/forum/search.php')['classification'], 'search_form_instead_of_results')
        self.assertEqual(row['forms'][0]['fields'], ['csrf', 'search_keywords'])
        for secret in ('DO_NOT_SHARE', 'PRIVATE_QUERY', 'PRIVATE_TOKEN'):
            self.assertNotIn(secret, str(row))

    def test_navigation_timeout_does_not_claim_browser_success(self):
        html = '<a href="/product/radio-123/">Радиостанция DMR</a>'
        row = summarize_navigation('ozon', html, 'https://www.ozon.ru/search/?text=DMR', [], 'TimeoutError')
        self.assertEqual(row['classification'], 'navigation_incomplete')

    def test_radiokot_posts_with_search_forms_are_results(self):
        html = '''<form action="./search.php"><input name="keywords"></form>
        <h3><a href="./viewtopic.php?t=123">LM358</a></h3>'''
        row = inspect_document('radiokot', html, 'https://radiokot.ru/forum/search.php?keywords=LM358')
        self.assertEqual(row['classification'], 'results')
        self.assertEqual(row['candidate_count'], 1)

    def test_forum_links_do_not_prove_search_form_only(self):
        html = '''<form><input name="keywords"></form>
        <a class="arrow-right" href="./viewtopic.php?t=123"></a>'''
        row = inspect_document('radiokot', html, 'https://radiokot.ru/forum/search.php?keywords=LM358')
        self.assertEqual(row['classification'], 'forum_layout_unrecognized')
