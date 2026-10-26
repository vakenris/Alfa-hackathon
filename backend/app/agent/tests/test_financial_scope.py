"""Local financial unknownness/negation; factual guards remain independent."""
import json
import unittest
from unittest.mock import patch

from backend.app.agent import AgentInsightEngine, FakeLLMClient, RecordingTracer
from backend.app.agent.config import AgentConfig
from backend.app.agent.context import prepare_context
from backend.app.agent.models import DraftInsight
from backend.app.agent.tests.test_grounded import fixtures, valid_draft
from backend.app.agent.verification import verify_draft


class FinancialScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        network=patch('socket.socket.connect',side_effect=AssertionError('NETWORK_FORBIDDEN'))
        network.start();self.addCleanup(network.stop)
        self.company,self.event=fixtures()
        self.context=await prepare_context(self.company,self.event,[],None,AgentConfig())

    def value_in_field(self,text,field):
        value=valid_draft()
        if field=='mechanism':
            value['impacts'][0]['mechanism']=text
            path='impacts[0].mechanism'
        elif field=='outcome':
            value['scenarios']=[{'label':'Проверка чувствительности',
                'condition':'Если компания рассматривает новое финансирование',
                'outcome':text,'evidence_ids':['news:event','company:mainokved_code']}]
            path='scenarios[0].outcome'
        else:
            value['actions'][0]['rationale']=text
            path='actions[0].rationale'
        return value,path

    def issues(self,text,field='mechanism'):
        value,path=self.value_in_field(text,field)
        report=verify_draft(DraftInsight.model_validate(value),self.context)
        return report.issues,path

    def test_live_B01_disclaimer_and_local_unknownness_are_not_company_facts(self):
        for text in (
            'Флаг лизингового продукта у компании не подтверждает получение кредита, остаток долга, тип ставки или условия.',
            'Флаг продукта не доказывает наличие фиксированной или плавающей ставки.',
            'Остаток долга неизвестен.',
            'Фиксированная ставка компании неизвестна.',
            'Неизвестны остаток долга и условия кредитования.',
            'Нет сведений о плавающей ставке.',
            'Неизвестны фиксированная ставка и остаток долга, которые могут влиять на расходы финансирования.',
            'Нужно понять, есть ли заимствования с плавающей ставкой или потребность в рефинансировании.',
        ):
            for field in ('mechanism','outcome','rationale'):
                with self.subTest(text=text,field=field):
                    issues,_=self.issues(text+' Стоимость финансирования может влиять на выбор вариантов.',field)
                    self.assertEqual(issues,[])

    def test_existing_conditional_contract_and_analytic_action_stay_valid(self):
        value=valid_draft()
        value['impacts'][0]['condition']='Если у компании есть обязательства с плавающей ставкой'
        value['actions'][0]['title']='Сверка условий фиксированных и плавающих ставок'
        self.assertTrue(verify_draft(DraftInsight.model_validate(value),self.context).passed)

    def test_negation_and_unknownness_do_not_extend_to_later_financial_assertions(self):
        endings=(
            '. Стоимость обслуживания плавающей ставки может влиять на расходы.',
            '; остаток долга может влиять на стоимость финансирования.',
            ': остаток долга растёт и может повлиять на стоимость финансирования.',
            ', но остаток долга может влиять на стоимость финансирования.',
            ', а фиксированная ставка действует по договору и может влиять на расходы.',
            ', и фиксированная ставка действует по договору и может влиять на расходы.',
            ' и остаток долга влияет на расходы, которые могут зависеть от финансирования.',
            ', стоимость обслуживания плавающей ставки может влиять на расходы.',
        )
        for start in ('Флаг не подтверждает остаток долга','Неизвестен остаток долга'):
            for ending in endings:
                for field in ('mechanism','outcome','rationale'):
                    with self.subTest(start=start,ending=ending,field=field):
                        issues,path=self.issues(start+ending,field)
                        self.assertIn(('UNSUPPORTED_COMPANY_CONDITION',path),[(i.code,i.path) for i in issues])

    def test_disclaimer_does_not_hide_independent_dangerous_content(self):
        cases=(
            ('У компании есть кредитный долг, что может повлиять на бюджет.','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Компания получила кредит, что может повлиять на бюджет.','UNSUPPORTED_FACTUAL_PREMISE'),
            ('Экономия может составить 5%.','UNSUPPORTED_INFERENCE_NUMBER'),
            ('Банк рекомендует кредит, который может поддержать инвестиции.','UNSUPPORTED_BANK_ATTRIBUTION'),
            ('Расходы гарантированно снизятся.','FALSE_CERTAINTY'),
            ('Признак продукта подтверждает наличие долга, который может влиять на бюджет.','UNSUPPORTED_FACTUAL_PREMISE'),
        )
        for text,code in cases:
            for field in ('mechanism','outcome','rationale'):
                with self.subTest(text=text,field=field):
                    issues,path=self.issues('Флаг не подтверждает остаток долга. '+text,field)
                    self.assertIn((code,path),[(i.code,i.path) for i in issues])
        value=valid_draft();value['actions'][0]['title']='Сверить долг и подписать кредитный договор'
        value['actions'][0]['rationale']='Флаг не подтверждает остаток долга.'
        report=verify_draft(DraftInsight.model_validate(value),self.context)
        self.assertIn('UNSUPPORTED_ACTION',[i.code for i in report.issues])

    def test_question_ends_at_comma_and_unknownness_cannot_be_elsewhere(self):
        for text in (
            'Нужно понять, есть ли финансирование, стоимость обслуживания плавающей ставки может влиять на расходы.',
            'Стоимость обслуживания плавающей ставки может влиять на расходы; остаток долга неизвестен.',
        ):
            with self.subTest(text=text):
                issues,_=self.issues(text)
                self.assertIn('UNSUPPORTED_COMPANY_CONDITION',[i.code for i in issues])

    def test_analytical_request_cannot_hide_later_financial_assertions(self):
        for text in (
            'Проверить условия. Остаток долга может влиять на выбор финансирования.',
            'Сверка условий; плавающая ставка применяется по кредиту и может влиять на расходы.',
            'Проверить условия и остаток долга может влиять на расходы.',
        ):
            with self.subTest(text=text):
                issues,_=self.issues(text,'rationale')
                self.assertIn('UNSUPPORTED_COMPANY_CONDITION',[i.code for i in issues])

    def test_regex_issues_identify_late_numbers_facts_certainty_and_attribution(self):
        prefix='Условия финансирования требуют проверки перед выбором вариантов. '*4
        for text,code,fragment in (
            ('Экономия может составить 5%.','UNSUPPORTED_INFERENCE_NUMBER','5'),
            ('Компания получила кредит, что может влиять на бюджет.','UNSUPPORTED_FACTUAL_PREMISE','получила'),
            ('Расходы гарантированно снизятся.','FALSE_CERTAINTY','гарантированно'),
            ('Банк рекомендует кредит, который может влиять на бюджет.','UNSUPPORTED_BANK_ATTRIBUTION','Банк рекомендует'),
        ):
            with self.subTest(code=code):
                issues,_=self.issues(prefix+text)
                issue=next(i for i in issues if i.code==code)
                self.assertIn(fragment,issue.excerpt)
                self.assertLessEqual(len(issue.excerpt),160)

    async def test_exact_live_disclaimer_needs_one_call_and_no_repair(self):
        text=(
            'Событие фиксирует сохранение ключевой ставки, а не её повышение или снижение. '
            'Само по себе это не создаёт нового повышательного импульса, но и не отменяет действующие платежи '
            'и не доказывает изменение расходов. Кредитная ставка может отличаться от ключевой; '
            'сохранение ключевой не меняет платежи автоматически. '
            'Флаг лизингового продукта у компании не подтверждает получение кредита, остаток долга, тип ставки или условия; '
            'он лишь указывает на возможный канал, требующий проверки. '
            'Производственный профиль не определяет наличие долга или ставку. '
            'Неизвестный долг не позволяет рассчитать эффект, поэтому вывод условный.')
        value,_=self.value_in_field(text,'mechanism')
        client=FakeLLMClient(json.dumps(value,ensure_ascii=False))
        result=await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertTrue(result.verification.passed)
        self.assertEqual(len(client.prompts),1)
        self.assertEqual(result.report['validation_summary']['repairs'],0)

    async def test_late_financial_marker_recovery_receives_the_actual_offending_text(self):
        prefix=('Условия финансирования требуют проверки перед выбором вариантов. '*4)
        text=prefix+'Стоимость обслуживания плавающей ставки может влиять на расходы.'
        bad,path=self.value_in_field(text,'mechanism')
        issues=verify_draft(DraftInsight.model_validate(bad),self.context).issues
        issue=next(i for i in issues if i.code=='UNSUPPORTED_COMPANY_CONDITION')
        self.assertEqual(issue.path,path)
        self.assertIn('плавающей ставки',issue.excerpt)
        self.assertLessEqual(len(issue.excerpt),160)
        client=FakeLLMClient(responses=[json.dumps(bad,ensure_ascii=False),json.dumps(valid_draft(),ensure_ascii=False)])
        result=await AgentInsightEngine(client).generate_analysis(self.company,self.event,[],RecordingTracer())
        self.assertTrue(result.verification.passed)
        self.assertEqual(len(client.prompts),2)
        repair_issues=json.loads(client.prompts[1].split('Нарушения (точное поле, фрагмент и код):\n',1)[1].split('\nПредыдущий ответ',1)[0])
        sent=next(i for i in repair_issues if i['code']=='UNSUPPORTED_COMPANY_CONDITION')
        self.assertEqual(sent['excerpt'],issue.excerpt)
        self.assertEqual(sent['path'],path)


if __name__=='__main__': unittest.main()
