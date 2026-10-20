from unittest import TestCase

from ui import display_team


class TeamDisplayNameTests(TestCase):
    def test_visible_serie_a_and_la_liga_names_use_traditional_chinese(self):
        self.assertEqual(display_team("Cagliari"), "卡利亞里")
        self.assertEqual(display_team("Monza"), "蒙札")
        self.assertEqual(display_team("Torino"), "拖連奴")
        self.assertEqual(display_team("Parma"), "帕爾馬")
        self.assertEqual(display_team("Rayo Vallecano"), "巴列卡諾")
        self.assertEqual(display_team("Getafe"), "赫塔費")

    def test_aliases_and_unknown_provider_names_are_safe(self):
        self.assertEqual(display_team("Fenerbahçe"), "費倫巴治")
        self.assertEqual(display_team("Tottenham Hotspur"), "熱刺")
        self.assertEqual(display_team("Unlisted Club"), "Unlisted Club")
