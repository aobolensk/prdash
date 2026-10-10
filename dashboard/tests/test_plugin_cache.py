from unittest.mock import patch

from django.test import SimpleTestCase

from prdash import plugin_cache


@patch.dict('prdash.plugin_cache._CACHE', clear=True)
@patch.object(plugin_cache, 'MAX_ENTRIES', 3)
class PluginCacheEvictionTests(SimpleTestCase):
    def test_oldest_entries_dropped_past_cap(self):
        for i in range(5):
            plugin_cache.cache_set(f'k{i}', i, 60)

        self.assertEqual(list(plugin_cache._CACHE), ['k2', 'k3', 'k4'])

    def test_expired_entries_swept_before_live_ones(self):
        with patch('prdash.plugin_cache.time.time', return_value=0):
            plugin_cache.cache_set('expired', 1, 10)
            plugin_cache.cache_set('live-a', 2, 1000)
            plugin_cache.cache_set('live-b', 3, 1000)
        with patch('prdash.plugin_cache.time.time', return_value=100):
            plugin_cache.cache_set('live-c', 4, 1000)

        self.assertEqual(set(plugin_cache._CACHE), {'live-a', 'live-b', 'live-c'})

    def test_resetting_a_key_refreshes_its_position(self):
        for key in ('a', 'b', 'c'):
            plugin_cache.cache_set(key, key, 60)
        plugin_cache.cache_set('a', 'a2', 60)
        plugin_cache.cache_set('d', 'd', 60)

        self.assertEqual(set(plugin_cache._CACHE), {'c', 'a', 'd'})
        self.assertEqual(plugin_cache.cache_get('a'), 'a2')
