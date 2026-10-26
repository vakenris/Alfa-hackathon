"""Model-facing packing keeps every source, constraint and search permission."""
import copy
import json
import unittest
from unittest.mock import patch

from backend.app.agent.config import AgentConfig
from backend.app.agent.context import json_text, prepare_context
from backend.app.agent.models import AgentContextError
from backend.app.agent.prompts import build_prompt, packed_context
from backend.app.agent.tests.test_grounded import fixtures
from backend.app.schemas.company import CounterpartyItem


class PromptContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        guard=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        guard.start();self.addCleanup(guard.stop)
        company,event=fixtures()
        peers=[CounterpartyItem(synthetic_u='OTHER',direction='BENEFICIARY',
                                total_amount=50,transaction_count=2)]
        self.context=await prepare_context(company,event,peers,None,AgentConfig())
        self.payload=self.context.payload
        self.payload['calculations']=[{'id':'calculation:test','value':42,'limitations':['Входные допущения.']}]
        self.payload['agent_context']={
            'plan':{'focus_channels':['financing','investment']},
            'observations':[{'tool':'summarize_payments','evidence_ids':['payments:summary']}],
            'adaptations':[], 'can_request_bank_search':True,
            'remaining_searches':1,'remaining_bank_documents':2,'remaining_llm_calls':2,
        }

    def test_initial_and_reanalysis_pack_is_lossless_and_preserves_search_permissions(self):
        original=copy.deepcopy(self.payload)
        for remaining_calls,can_search in ((2,True),(1,False)):
            with self.subTest(remaining_calls=remaining_calls):
                payload=copy.deepcopy(original)
                payload['agent_context'].update(remaining_llm_calls=remaining_calls,
                                                can_request_bank_search=can_search)
                before=copy.deepcopy(payload)
                prompt=build_prompt(payload)
                sent=json.loads(prompt.split('Входные данные (JSON):\n',1)[1])
                restored=[{**sent['source_metadata'][item['source_id']],**item}
                          for item in sent['evidence']]
                self.assertEqual(restored,payload['evidence'])
                self.assertEqual(sent['agent_context'],payload['agent_context'])
                for key in ('company','business_context','event','warnings','calculations'):
                    self.assertEqual(sent[key],payload[key])
                # The complete scope remains in business limitations; no raw
                # payment row, quote or denominator is shortened to save space.
                self.assertIn(payload['payment_scope'],sent['business_context']['limitations'])
                self.assertLess(len(json_text(sent)),len(json_text(payload)))
                self.assertEqual(payload,before)
        self.assertEqual(self.payload,original)

    def test_nonduplicate_scope_is_kept(self):
        self.payload['payment_scope']='Дополнительное ограничение отдельной выборки.'
        self.assertEqual(packed_context(self.payload)['payment_scope'],self.payload['payment_scope'])

    def test_initial_conflicting_provenance_fails_closed(self):
        item=copy.deepcopy(next(item for item in self.payload['evidence'] if item['kind']=='company'))
        item.update(id='company:conflict',provenance_status='verified')
        self.payload['evidence'].append(item)
        with self.assertRaisesRegex(AgentContextError,'SOURCE_METADATA_CONFLICT'):
            build_prompt(self.payload)

    def test_initial_size_limits_still_checked_before_request_without_truncation(self):
        prompt=build_prompt(self.payload)
        size=len(json_text(packed_context(self.payload)))
        self.assertEqual(build_prompt(self.payload,max_context_chars=size,max_prompt_chars=len(prompt)),prompt)
        for limits in ({'max_context_chars':size-1},{'max_prompt_chars':len(prompt)-1}):
            with self.subTest(limits=limits),self.assertRaisesRegex(AgentContextError,'PROMPT_LIMIT'):
                build_prompt(self.payload,**limits)


if __name__=='__main__': unittest.main()
