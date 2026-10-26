"""Deterministic top-three concentration, bounded to each passed payment direction."""

import math
import unittest

from backend.app.agent.context import _payments
from backend.app.agent.models import AgentContextError
from backend.app.schemas.company import CompanyResponse, CounterpartyItem


class PaymentConcentrationTests(unittest.TestCase):
    def setUp(self):
        self.company = CompanyResponse(id=1, synthetic_u="OWN", name="Synthetic company")

    def row(self, uid, amount, direction="OUTGOING", count=1):
        return CounterpartyItem(synthetic_u=uid, direction=direction,
                                total_amount=amount, transaction_count=count)

    def summary(self, rows):
        return _payments(self.company, rows)[0]["directions"]

    def test_largest_three_share_uses_entire_passed_direction_not_selected_peers(self):
        amounts = [368, 227, 115, 100, 90, 70, 30]
        directions = self.summary([self.row(f"PEER_{index}", amount)
                                   for index, amount in enumerate(amounts)])
        outgoing = directions["outgoing"]
        self.assertEqual(outgoing["total_amount"], 1000)
        self.assertEqual(outgoing["unique_counterparties"], 7)
        self.assertEqual(outgoing["largest_group"], {
            "counterparties": 3, "total_amount": 710,
            "share_of_passed_direction_amount": .71,
            "synthetic_us": ["PEER_0", "PEER_1", "PEER_2"],
        })

    def test_fewer_than_three_peers_are_counted_without_inventing_others(self):
        directions = self.summary([self.row("A", .1, "INCOMING"),
                                   self.row("B", .2, "INCOMING")])
        largest = directions["incoming"]["largest_group"]
        self.assertEqual(largest["counterparties"], 2)
        self.assertEqual(largest["total_amount"], math.fsum([.1, .2]))
        self.assertEqual(largest["share_of_passed_direction_amount"], 1)
        self.assertEqual(largest["synthetic_us"], ["B", "A"])

    def test_zero_and_empty_directions_have_no_defined_share(self):
        directions = self.summary([self.row("B", 0), self.row("A", 0)])
        self.assertEqual(directions["outgoing"]["largest_group"], {
            "counterparties": 2, "total_amount": 0,
            "share_of_passed_direction_amount": None, "synthetic_us": ["A", "B"],
        })
        self.assertEqual(directions["incoming"]["largest_group"], {
            "counterparties": 0, "total_amount": 0,
            "share_of_passed_direction_amount": None, "synthetic_us": [],
        })

    def test_own_transfers_unknown_and_opposite_direction_never_dilute_share(self):
        directions = self.summary([self.row("OWN", 1000, "SUPPLIER"),
                                   self.row("UNKNOWN", 500, "unrecognized"),
                                   self.row("A", 20), self.row("A", 90, "PAYER")])
        self.assertEqual(directions["self_transfer"]["total_amount"], 1000)
        self.assertEqual(directions["unknown"]["total_amount"], 500)
        self.assertNotIn("largest_group", directions["self_transfer"])
        self.assertNotIn("largest_group", directions["unknown"])
        for direction, amount in (("incoming", 90), ("outgoing", 20)):
            largest = directions[direction]["largest_group"]
            self.assertEqual(largest["total_amount"], amount)
            self.assertEqual(largest["share_of_passed_direction_amount"], 1)
            self.assertEqual(largest["synthetic_us"], ["A"])

    def test_aliases_merge_before_ranking_and_top_evidence_matches_group_members(self):
        rows = [self.row("A", 10, "BENEFICIARY"), self.row("A", 30, "SUPPLIER"),
                self.row("B", 30), self.row("C", 20), self.row("D", 10)]
        snapshots = [item.model_dump() for item in rows]
        summary, top = _payments(self.company, rows)
        outgoing = summary["directions"]["outgoing"]
        largest = outgoing["largest_group"]
        self.assertEqual(outgoing["unique_counterparties"], 4)
        self.assertEqual(largest["counterparties"], 3)
        self.assertEqual(largest["total_amount"], 90)
        self.assertEqual(largest["share_of_passed_direction_amount"], .9)
        self.assertEqual(largest["synthetic_us"], ["A", "B", "C"])
        self.assertEqual([item["synthetic_u"] for _, item in top], largest["synthetic_us"])
        self.assertEqual([item.model_dump() for item in rows], snapshots)

    def test_invalid_or_overflowing_amounts_are_still_rejected(self):
        for amount in (float("nan"), float("inf"), float("-inf"), -1):
            with self.subTest(amount=amount), self.assertRaises(AgentContextError):
                self.summary([self.row("A", amount)])
        with self.assertRaises(AgentContextError):
            self.summary([self.row("A", 1e308), self.row("B", 1e308)])


if __name__ == "__main__":
    unittest.main()
