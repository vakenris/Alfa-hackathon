"""Internal prose bounds do not spend recovery on an otherwise valid mechanism."""
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from backend.app.agent import AgentInsightEngine, FakeLLMClient, RecordingTracer
from backend.app.agent.models import DraftInsight
from backend.app.agent.tests.test_grounded import fixtures, valid_draft


class MechanismLengthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        network.start();self.addCleanup(network.stop)
        self.company,self.event=fixtures()

    def long_draft(self):
        value=valid_draft()
        value['impacts'][0]['mechanism']=(
            'Стоимость нового финансирования может влиять на выбор этапности инвестиций. '*8).strip()
        self.assertGreater(len(value['impacts'][0]['mechanism']),500)
        self.assertLess(len(value['impacts'][0]['mechanism']),1000)
        return value

    def test_only_mechanism_gets_a_larger_strict_bound(self):
        value=valid_draft();value['impacts'][0]['mechanism']='a'*1000
        self.assertEqual(len(DraftInsight.model_validate(value).impacts[0].mechanism),1000)
        for mechanism in ('a'*1001,'',None,1000):
            with self.subTest(mechanism_type=type(mechanism).__name__),self.assertRaises(ValidationError):
                DraftInsight.model_validate({**value,'impacts':[{**value['impacts'][0],'mechanism':mechanism}]})
        value['impacts'][0]['condition']='a'*501
        with self.assertRaises(ValidationError) as caught:
            DraftInsight.model_validate(value)
        self.assertEqual(caught.exception.errors()[0]['loc'],('impacts',0,'condition'))

    async def test_valid_long_mechanism_needs_one_call_and_survives_grounded_qa(self):
        from backend.app.agent import ManagementActionEngine

        value=self.long_draft();client=FakeLLMClient(json.dumps(value))
        result=await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertTrue(result.verification.passed)
        self.assertEqual(result.analysis.impacts[0].mechanism,value['impacts'][0]['mechanism'])
        self.assertEqual(len(client.prompts),1)
        self.assertEqual(result.report['validation_summary']['repairs'],0)
        answer=await ManagementActionEngine(None).answer_question(
            'Какие источники и последствия?',analysis=result,company=self.company,tracer=RecordingTracer())
        self.assertEqual(answer.status,'complete')
        self.assertEqual(len(client.prompts),1)

    async def test_factual_violation_after_old_boundary_still_requires_repair(self):
        good=self.long_draft();bad=self.long_draft()
        bad['impacts'][0]['mechanism']+=' Компания получила кредит.'
        client=FakeLLMClient(responses=[json.dumps(bad),json.dumps(good)])
        result=await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertTrue(result.verification.passed)
        self.assertEqual(len(client.prompts),2)
        self.assertEqual(result.report['validation_attempts'][0]['issue_codes'],['UNSUPPORTED_FACTUAL_PREMISE'])
        self.assertEqual(result.report['validation_summary']['repairs'],1)
        self.assertIn('UNSUPPORTED_FACTUAL_PREMISE',client.prompts[1])


if __name__=='__main__': unittest.main()
