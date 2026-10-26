"""Channel names cannot stand in for an event-to-mechanism bridge; no I/O."""
import unittest
from unittest.mock import patch

from backend.app.agent.config import AgentConfig
from backend.app.agent.context import prepare_context
from backend.app.agent.models import DraftInsight
from backend.app.agent.planning import channels_for_text, supports_causal_channel
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.agent.verification import verify_draft


class ChannelLabelGroundingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        guard.start();self.addCleanup(guard.stop)
        self.company,self.event=fixtures()
        self.context=await prepare_context(self.company,self.event,[],None,AgentConfig())

    def test_unrelated_body_is_rejected_under_every_relabel(self):
        for channel in ('demand','financing','investment','other'):
            with self.subTest(channel=channel):
                draft=valid_draft()
                draft['impacts'][0].update(channel=channel,mechanism='Качество стен может улучшиться.')
                report=verify_draft(DraftInsight.model_validate(draft),self.context)
                self.assertIn(('UNSUPPORTED_CAUSAL_CHANNEL','impacts[0].channel'),
                              [(issue.code,issue.path) for issue in report.issues])

    def test_financing_planning_default_does_not_establish_investment_bridge(self):
        self.assertIn('investment',channels_for_text(self.event.content))
        self.assertFalse(supports_causal_channel('investment',self.event.content,
            'Если компания планирует инвестиции',
            'Инвестиции могут улучшить производственные процессы.'))

    def test_upstream_topic_in_condition_alone_does_not_support_body(self):
        for channel in ('demand','financing','other'):
            with self.subTest(channel=channel):
                self.assertFalse(supports_causal_channel(channel,self.event.content,
                    'Если компания рассматривает новое кредитное финансирование',
                    'Спрос может зависеть от предпочтений покупателей.'))

    def test_direct_and_indirect_economic_mechanisms_still_pass(self):
        for channel,mechanism in (
            ('financing','Стоимость банковского фондирования может влиять на доступность нового финансирования.'),
            ('investment','Стоимость нового кредита может влиять на выбор этапности инвестиций.'),
            ('demand','Доступность ипотечного финансирования может влиять на спрос на строительные проекты.'),
            ('other','Стоимость кредита может влиять на выбор вариантов работы.'),
        ):
            with self.subTest(channel=channel):
                self.assertTrue(supports_causal_channel(channel,self.event.content,
                    'Если компания зависит от затронутых условий финансирования',mechanism))

    def test_topics_outside_vocabulary_can_use_actual_event_wording(self):
        self.assertTrue(supports_causal_channel('other',
            'Изменились условия обращения лекарственных препаратов.',
            'Если компания выпускает затронутые лекарственные препараты',
            'Условия обращения лекарственных препаратов могут влиять на выбор вариантов работы.'))

    def test_saved_B01_cost_of_capital_to_investment_is_an_explicit_bridge(self):
        self.assertTrue(supports_causal_channel('investment',self.event.content,
            'Если инвестиционная программа компании зависит от внешнего долгового финансирования или лизинга оборудования.',
            'Стоимость капитала может влиять на окупаемость и сроки проектов, что может сдвинуть приоритеты модернизации и закупок оборудования.'))

    def test_existing_explicit_sector_reference_is_preserved(self):
        condition='Если деятельность компании связана с затронутым сегментом'
        mechanism='Изменение условий в затронутом сегменте может влиять на планы и выбор вариантов работы.'
        self.assertTrue(supports_causal_channel('inputs',
            'Стоимость стального проката выросла.',condition,mechanism))
        self.assertTrue(supports_causal_channel('other',
            'Изменились условия обращения лекарственных препаратов.',condition,mechanism))
        self.assertFalse(supports_causal_channel('other',
            'Завершился шахматный турнир.',condition,mechanism))

    def test_generic_segment_does_not_bypass_monetary_bridge_under_any_label(self):
        for channel in ('demand','financing','investment','other'):
            with self.subTest(channel=channel):
                self.assertFalse(supports_causal_channel(channel,self.event.content,
                    'Если деятельность компании связана с затронутым сегментом',
                    'Качество стен в затронутом сегменте может улучшиться.'))


if __name__=='__main__':unittest.main()
