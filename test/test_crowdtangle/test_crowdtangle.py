import unittest
from unittest import mock

import requests_mock

from parsons import CrowdTangle, Table
from test.conftest import assert_matching_tables
from test.test_crowdtangle.leaderboard import expected_leaderboard
from test.test_crowdtangle.link_post import expected_post
from test.test_crowdtangle.post import expected_posts

CT_API_KEY = "FAKE_KEY"


class TestCrowdTangle(unittest.TestCase):
    def setUp(self):
        self.ct = CrowdTangle(CT_API_KEY)

    @requests_mock.Mocker()
    def test_get_posts(self, m):
        m.get(self.ct.uri + "/posts", json=expected_posts)
        posts = self.ct.get_posts()
        exp_tbl = self.ct._unpack(Table(expected_posts["result"]["posts"]))
        assert_matching_tables(posts, exp_tbl)

    # Patch the shared client's sleep so rate_limit_interval doesn't stall the test.
    @mock.patch("parsons.utilities.api_connector._sleep")
    @requests_mock.Mocker()
    def test_get_posts_paginates_via_next_page(self, mock_sleep, m):
        post = expected_posts["result"]["posts"][0]
        next_url = "https://api.crowdtangle.com/posts?page=2"
        page1 = {"result": {"posts": [post], "pagination": {"nextPage": next_url}}}
        page2 = {"result": {"posts": [post, post], "pagination": {}}}
        m.get(self.ct.uri + "/posts", json=page1)  # initial request
        m.get(next_url, json=page2)  # followed from result.pagination.nextPage

        posts = self.ct.get_posts()

        # All three rows across the two pages are concatenated.
        assert posts.num_rows == 3
        assert m.call_count == 2
        # The second request is the absolute nextPage URL, and the sleep fired once.
        assert m.request_history[1].url == next_url
        assert mock_sleep.call_count == 1

    @requests_mock.Mocker()
    def test_get_leaderboard(self, m):
        m.get(self.ct.uri + "/leaderboard", json=expected_leaderboard)
        leaderboard = self.ct.get_leaderboard()
        exp_tbl = self.ct._unpack(Table(expected_leaderboard["result"]["accountStatistics"]))
        assert_matching_tables(leaderboard, exp_tbl)

    @requests_mock.Mocker()
    def test_get_links(self, m):
        m.get(self.ct.uri + "/links", json=expected_post)
        post = self.ct.get_links(link="https://nbcnews.to/34stfC2")
        exp_tbl = self.ct._unpack(Table(expected_post["result"]["posts"]))
        assert_matching_tables(post, exp_tbl)
