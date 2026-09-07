"""
NeuroStack — Intent Classifier Accuracy Test
200-case patient message dataset (MVP v0.6 expected responses).

Maps each dataset category to the expected official intent.
Run: pytest tests/srm/test_intent_accuracy.py -v
"""

import pytest

from src.srm.intent_classifier import IntentClassifier

# ============================================
# Dataset: message → expected_intent
# Extracted from 200-case "Respostas Previstas" document
# ============================================

DATASET: list[tuple[str, str]] = [
    # ── SAUDAÇÃO (6 cases) → saudacao ──
    ("Oi", "saudacao"),
    ("Bom dia!", "saudacao"),
    ("Boa tarde, tudo bem?", "saudacao"),
    ("Olá, vocês ainda estão atendendo?", "saudacao"),
    ("Oii", "saudacao"),
    ("Boa noite", "saudacao"),
    # ── INTERESSE EM TRATAMENTO (8 cases) → interesse_tratamento ──
    ("Quero saber sobre tratamentos estéticos", "interesse_tratamento"),
    ("Vocês fazem botox?", "interesse_tratamento"),
    ("Tenho interesse em clareamento dental", "interesse_tratamento"),
    ("Quero marcar uma avaliação", "interesse_tratamento"),
    ("Vocês atendem dermatologia?", "interesse_tratamento"),
    ("Quero tratar minha queda de cabelo", "interesse_tratamento"),
    ("Tenho interesse em fazer harmonização facial", "interesse_tratamento"),
    ("Quero tratar minhas manchas no rosto", "interesse_tratamento"),
    # ── QUALIFICAÇÃO (28 cases) → interesse_tratamento (qualify = deeper interest) ──
    ("Nunca fiz botox, quero entender como funciona", "interesse_tratamento"),
    ("Já fiz botox em outro lugar mas não gostei do resultado", "interesse_tratamento"),
    ("Já faço botox regularmente e quero continuar aqui", "interesse_tratamento"),
    ("Quero fazer uma consulta com ortopedista", "interesse_tratamento"),
    ("Me explica como funciona", "interesse_tratamento"),
    ("Quero saber mais", "interesse_tratamento"),
    (
        "Tenho 45 anos e quero cuidar mais da pele, por onde começo?",
        "interesse_tratamento",
    ),
    ("Quero emagrecer, vocês têm tratamento?", "interesse_tratamento"),
    ("Quero fazer peeling", "interesse_tratamento"),
    ("Tenho acne e quero tratar", "interesse_tratamento"),
    ("Quero melhorar meu sorriso", "interesse_tratamento"),
    ("Tenho bruxismo e quero tratar", "interesse_tratamento"),
    ("Tenho 30 anos e nunca fui ao dentista, tenho vergonha", "interesse_tratamento"),
    (
        "Tive uma experiência ruim no dentista antes e tenho medo",
        "interesse_tratamento",
    ),
    (
        "Já fiz harmonização em outro lugar, quero continuar aqui",
        "interesse_tratamento",
    ),
    ("Sofro de ansiedade, isso interfere no tratamento?", "interesse_tratamento"),
    ("Sou diabético, posso fazer o procedimento?", "interesse_tratamento"),
    ("Estou grávida, vocês atendem?", "interesse_tratamento"),
    ("Sou alérgica a anestesia, vocês têm como atender?", "interesse_tratamento"),
    ("Queria entender melhor antes de decidir", "interesse_tratamento"),
    ("Não sei por onde começar", "interesse_tratamento"),
    ("Meu filho quer fazer um procedimento, tem idade mínima?", "interesse_tratamento"),
    ("Tenho endometriose, vocês têm tratamento?", "interesse_tratamento"),
    (
        "Quero tratar minha depressão, vocês atendem psiquiatria?",
        "interesse_tratamento",
    ),
    ("Tenho 60 anos, posso fazer botox?", "interesse_tratamento"),
    ("Quero tratar cicatriz de acne", "interesse_tratamento"),
    (
        "Fiz a mesma coisa na clínica X e fiquei satisfeita, quero fazer aqui",
        "interesse_tratamento",
    ),
    ("Ouvi falar do tratamento X, vocês fazem?", "interesse_tratamento"),
    # ── PEDIDO DE PREÇO (14 cases) → pedido_preco ──
    ("Quanto custa o botox?", "pedido_preco"),
    ("Qual o valor da consulta?", "pedido_preco"),
    ("Quanto fica o clareamento dental?", "pedido_preco"),
    ("Mas me dá pelo menos uma faixa de preço", "pedido_preco"),
    ("Vocês parcelam?", "pedido_preco"),
    ("Tem desconto?", "pedido_preco"),
    ("Qual o preço de uma consulta com dermatologista?", "pedido_preco"),
    ("Tem pacote de procedimentos?", "pedido_preco"),
    ("O botox de vocês é mais caro que da clínica X?", "pedido_preco"),
    ("Quanto custa a sessão de laser?", "pedido_preco"),
    ("Qual o valor do preenchimento labial?", "pedido_preco"),
    ("Tem como fazer por um preço menor?", "pedido_preco"),
    ("Aceita pix?", "pedido_preco"),
    ("Quais as formas de pagamento?", "pedido_preco"),
    # ── AGENDAMENTO (24 cases) → agendar ──
    ("Quero agendar uma consulta", "agendar"),
    ("Tem horário disponível para segunda?", "agendar"),
    ("Quero marcar para semana que vem", "agendar"),
    ("Tem vaga para amanhã?", "agendar"),
    ("Posso agendar para sábado?", "agendar"),
    ("Quero agendar uma avaliação gratuita", "agendar"),
    ("Tem horário de manhã?", "agendar"),
    ("Consigo um horário ainda essa semana?", "agendar"),
    ("Quero marcar uma consulta com a Dra. Maria", "agendar"),
    ("Pode ser na terça às 14h?", "agendar"),
    ("Quero agendar o procedimento", "agendar"),
    ("Tem disponibilidade para o dia 15?", "agendar"),
    ("Quero marcar minha primeira consulta", "agendar"),
    ("Tem horário à tarde?", "agendar"),
    ("Vamos agendar então", "agendar"),
    ("Pode marcar pra mim?", "agendar"),
    ("Quero garantir um horário", "agendar"),
    ("Quando posso ir?", "agendar"),
    ("Vocês atendem no fim de semana?", "agendar"),
    ("Tem horário noturno?", "agendar"),
    ("Quero marcar para o mais cedo possível", "agendar"),
    ("Pode ser na próxima quarta?", "agendar"),
    ("Quero agendar com urgência", "agendar"),
    ("Tem encaixe para hoje?", "agendar"),
    # ── INFORMAÇÕES DA CLÍNICA (23 cases) → detalhes_servicos / informacoes_profissionais / disponibilidade_horarios ──
    ("Onde fica a clínica?", "detalhes_servicos"),
    ("Qual o endereço?", "detalhes_servicos"),
    ("Como chego aí?", "detalhes_servicos"),
    ("Tem estacionamento?", "detalhes_servicos"),
    ("Vocês aceitam convênio?", "detalhes_servicos"),
    ("Quais convênios vocês aceitam?", "detalhes_servicos"),
    ("Qual horário de funcionamento?", "disponibilidade_horarios"),
    ("Que horas vocês abrem?", "disponibilidade_horarios"),
    ("Vocês abrem no sábado?", "disponibilidade_horarios"),
    ("Até que horas atendem?", "disponibilidade_horarios"),
    ("Quais os serviços disponíveis?", "detalhes_servicos"),
    ("Vocês fazem cirurgia plástica?", "detalhes_servicos"),
    ("Tem raio-x na clínica?", "detalhes_servicos"),
    ("Quais especialidades vocês atendem?", "detalhes_servicos"),
    ("Quem são os médicos?", "informacoes_profissionais"),
    ("Qual a formação do Dr. João?", "informacoes_profissionais"),
    ("Vocês têm nutricionista?", "informacoes_profissionais"),
    ("O médico é especialista?", "informacoes_profissionais"),
    ("Tem Wi-Fi na clínica?", "detalhes_servicos"),
    ("A clínica é acessível para cadeirante?", "detalhes_servicos"),
    ("Posso levar acompanhante?", "detalhes_servicos"),
    ("Vocês atendem crianças?", "detalhes_servicos"),
    ("A clínica tem quantos anos?", "cultura_historia"),
    # ── PÓS-ATENDIMENTO DÚVIDAS (9 cases) → pos_atendimento_duvida ──
    ("Posso tomar sorvete depois da extração?", "pos_atendimento_duvida"),
    ("É normal ficar inchado?", "pos_atendimento_duvida"),
    ("Quanto tempo demora para cicatrizar?", "pos_atendimento_duvida"),
    ("Posso fazer exercício depois do procedimento?", "pos_atendimento_duvida"),
    ("O curativo pode molhar?", "pos_atendimento_duvida"),
    ("Quando posso voltar a comer normalmente?", "pos_atendimento_duvida"),
    ("Posso dirigir depois da anestesia?", "pos_atendimento_duvida"),
    ("É normal sentir dormência?", "pos_atendimento_duvida"),
    ("Quanto tempo o efeito do botox dura?", "pos_atendimento_duvida"),
    # ── PÓS-ATENDIMENTO URGENTE (11 cases) → pos_atendimento_sintomas_preocupantes ──
    (
        "Tô com muita dor depois do procedimento",
        "pos_atendimento_sintomas_preocupantes",
    ),
    (
        "Meu rosto inchou muito após a aplicação",
        "pos_atendimento_sintomas_preocupantes",
    ),
    ("Tive sangramento depois da cirurgia", "pos_atendimento_sintomas_preocupantes"),
    ("Estou com febre após o procedimento", "pos_atendimento_sintomas_preocupantes"),
    ("A região ficou roxa e está piorando", "pos_atendimento_sintomas_preocupantes"),
    (
        "Não consigo abrir a boca depois da extração",
        "pos_atendimento_sintomas_preocupantes",
    ),
    ("Saiu pus do local da cirurgia", "pos_atendimento_sintomas_preocupantes"),
    (
        "Tô passando muito mal depois do procedimento",
        "pos_atendimento_sintomas_preocupantes",
    ),
    ("Minha pressão caiu depois da anestesia", "pos_atendimento_sintomas_preocupantes"),
    (
        "Tenho dor intensa que não passa com remédio",
        "pos_atendimento_sintomas_preocupantes",
    ),
    ("O sangramento não para", "pos_atendimento_sintomas_preocupantes"),
    # ── PEDIDO DE ALTERAÇÃO DE MEDICAÇÃO (6 cases) → pedido_alterar_medicacao ──
    ("Quero diminuir a dose do remédio", "pedido_alterar_medicacao"),
    ("O remédio tá muito forte, posso tomar menos?", "pedido_alterar_medicacao"),
    (
        "Posso trocar o medicamento? Tô tendo efeito colateral",
        "pedido_alterar_medicacao",
    ),
    ("Quero parar de tomar esse remédio", "pedido_alterar_medicacao"),
    ("Posso tomar junto com outro medicamento?", "pedido_alterar_medicacao"),
    ("A dose tá fraca, posso tomar mais?", "pedido_alterar_medicacao"),
    # ── RECLAMAÇÕES (9 cases) → reclamacao ──
    ("O atendimento foi péssimo", "reclamacao"),
    ("Esperando há 40 minutos", "reclamacao"),
    ("Ninguém me respondeu até agora", "reclamacao"),
    ("Fui mal atendida pela recepcionista", "reclamacao"),
    ("O médico atrasou 1 hora", "reclamacao"),
    ("Não resolveram meu problema", "reclamacao"),
    ("Já liguei 3 vezes e ninguém atende", "reclamacao"),
    ("O resultado não ficou como prometido", "reclamacao"),
    ("Estou muito insatisfeita com o serviço", "reclamacao"),
    # ── CONDUTA INADEQUADA / AMEAÇA (6 cases) → conduta_inadequada_ameaca ──
    ("Vou processar vocês", "conduta_inadequada_ameaca"),
    ("Seu atendimento é uma merda, vou no Procon", "conduta_inadequada_ameaca"),
    ("Vou denunciar no CRM", "conduta_inadequada_ameaca"),
    ("Isso é um absurdo, vou chamar meu advogado", "conduta_inadequada_ameaca"),
    ("Vocês vão se arrepender, vou expor nas redes", "conduta_inadequada_ameaca"),
    ("Que clínica de merda", "conduta_inadequada_ameaca"),
    # ── PEDIDO DE ATENDENTE HUMANO (4 cases) → pedido_atendente_humano ──
    ("Quero falar com um atendente humano", "pedido_atendente_humano"),
    ("Não quero falar com robô", "pedido_atendente_humano"),
    ("Me transfere para alguém de verdade", "pedido_atendente_humano"),
    ("Tem como falar com uma pessoa?", "pedido_atendente_humano"),
    # ── DOCUMENTOS E COMPROVANTES (5 cases) → documentos_e_comprovantes ──
    ("Preciso do meu atestado", "documentos_e_comprovantes"),
    ("Manda a receita por aqui", "documentos_e_comprovantes"),
    ("Preciso de um comprovante de comparecimento", "documentos_e_comprovantes"),
    ("Pode enviar o laudo do exame?", "documentos_e_comprovantes"),
    ("Quero a nota fiscal do procedimento", "documentos_e_comprovantes"),
    # ── ORÇAMENTO EXISTENTE (2 cases) → orcamento_existente ──
    ("Já recebi um orçamento antes", "orcamento_existente"),
    ("Quero ver o orçamento que me passaram", "orcamento_existente"),
    # ── FINANCEIRO / COBRANÇA (3 cases) → cobranca_renegociacao ──
    ("Quero parcelar meu débito", "cobranca_renegociacao"),
    ("Preciso renegociar minha dívida", "cobranca_renegociacao"),
    ("Recebi uma cobrança, o que é?", "cobranca_renegociacao"),
    # ── OBJEÇÕES (18 cases) → objecao_vou_pensar ──
    ("Vou pensar e te retorno", "objecao_vou_pensar"),
    ("Achei caro, vou pesquisar", "objecao_vou_pensar"),
    ("Não sei se vale a pena", "objecao_vou_pensar"),
    ("Preciso falar com meu marido antes", "objecao_vou_pensar"),
    ("Tenho medo de fazer procedimento", "objecao_vou_pensar"),
    ("Não estou convencida ainda", "objecao_vou_pensar"),
    ("Vou ver com outra clínica primeiro", "objecao_vou_pensar"),
    ("E se eu não gostar do resultado?", "objecao_vou_pensar"),
    ("Já ouvi que dói muito", "objecao_vou_pensar"),
    ("Tenho receio dos efeitos colaterais", "objecao_vou_pensar"),
    ("Não tenho certeza se preciso disso", "objecao_vou_pensar"),
    ("É muito longe pra mim", "objecao_vou_pensar"),
    ("Não sei se confio nesse procedimento", "objecao_vou_pensar"),
    ("Já gastei muito com isso e não funcionou", "objecao_vou_pensar"),
    ("Minha amiga disse que não vale", "objecao_vou_pensar"),
    ("Preciso pesquisar mais", "objecao_vou_pensar"),
    ("Acho que não é o momento", "objecao_vou_pensar"),
    ("Tô sem grana agora", "objecao_vou_pensar"),
    # ── COMPARAÇÃO DE TRATAMENTOS (5 cases) → comparacao_tratamentos ──
    ("Qual a diferença entre botox e preenchimento?", "comparacao_tratamentos"),
    ("O que é melhor, laser ou peeling?", "comparacao_tratamentos"),
    ("Clareamento a laser ou moldeira, qual é melhor?", "comparacao_tratamentos"),
    ("Implante ou prótese, qual a diferença?", "comparacao_tratamentos"),
    ("Qual tratamento é mais indicado pro meu caso?", "comparacao_tratamentos"),
    # ── PACIENTE ATIVO (4 cases) → paciente_ativo ──
    ("Já sou paciente de vocês", "paciente_ativo"),
    ("Faço acompanhamento há 2 anos", "paciente_ativo"),
    ("Sou paciente do Dr. João", "paciente_ativo"),
    ("Já faço tratamento aqui na clínica", "paciente_ativo"),
    # ── FORA DO ESCOPO CLÍNICO (10 cases) → fora_do_escopo ──
    ("Vocês tratam depressão?", "fora_do_escopo"),
    ("Fazem exame de sangue?", "fora_do_escopo"),
    ("Vocês fazem cirurgia bariátrica?", "fora_do_escopo"),
    ("Atendem veterinário?", "fora_do_escopo"),
    ("Fazem exame de vista?", "fora_do_escopo"),
    ("Vocês vendem remédio?", "fora_do_escopo"),
    ("Tem laboratório de análises?", "fora_do_escopo"),
    ("Vocês fazem vacinação?", "fora_do_escopo"),
    ("Posso fazer ressonância aí?", "fora_do_escopo"),
    ("Vocês tratam animais?", "fora_do_escopo"),
    # ── FORA DO HORÁRIO (4 cases) → fora_do_horario ──
    ("Vocês atendem fora do horário?", "fora_do_horario"),
    ("Tô mandando mensagem de madrugada, vocês abrem que horas?", "fora_do_horario"),
    ("Vocês têm plantão?", "fora_do_horario"),
    ("Tem atendimento de emergência fora do horário?", "fora_do_horario"),
]


@pytest.fixture
def classifier():
    return IntentClassifier()


class TestIntentAccuracyByCategory:
    """Test accuracy per dataset category."""

    def _run_category(self, classifier, category_name: str, expected_intent: str):
        cases = [(msg, exp) for msg, exp in DATASET if exp == expected_intent]
        if not cases:
            pytest.skip(f"No cases for {expected_intent}")

        correct = 0
        failures = []
        for msg, expected in cases:
            result = classifier.classify(msg)
            if result.intent == expected:
                correct += 1
            else:
                failures.append(
                    f"  '{msg}' → got '{result.intent}' (conf={result.confidence:.2f}), expected '{expected}'"
                )
        accuracy = correct / len(cases) * 100
        return accuracy, correct, len(cases), failures

    def test_saudacao(self, classifier):
        acc, c, t, f = self._run_category(classifier, "Saudação", "saudacao")
        assert acc == 100, f"Saudação {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_interesse_tratamento(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Interesse/Qualificação", "interesse_tratamento"
        )
        assert acc >= 80, f"Interesse {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_pedido_preco(self, classifier):
        acc, c, t, f = self._run_category(classifier, "Pedido de Preço", "pedido_preco")
        assert acc >= 90, f"Preço {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_agendamento(self, classifier):
        acc, c, t, f = self._run_category(classifier, "Agendamento", "agendar")
        assert acc >= 80, f"Agendamento {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_pos_urgente(self, classifier):
        """Safety-critical: must be 100%."""
        acc, c, t, f = self._run_category(
            classifier, "Pós-Urgente", "pos_atendimento_sintomas_preocupantes"
        )
        assert acc == 100, f"SAFETY: Pós-Urgente {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_pos_duvida(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Pós-Dúvida", "pos_atendimento_duvida"
        )
        assert acc >= 70, f"Pós-Dúvida {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_objecoes(self, classifier):
        acc, c, t, f = self._run_category(classifier, "Objeções", "objecao_vou_pensar")
        assert acc >= 75, f"Objeções {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_reclamacao(self, classifier):
        acc, c, t, f = self._run_category(classifier, "Reclamação", "reclamacao")
        assert acc >= 80, f"Reclamação {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_conduta_inadequada(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Conduta", "conduta_inadequada_ameaca"
        )
        assert acc >= 80, f"Conduta {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_pedido_atendente_humano(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Humano", "pedido_atendente_humano"
        )
        assert acc >= 75, f"Humano {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_documentos(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Documentos", "documentos_e_comprovantes"
        )
        assert acc >= 75, f"Documentos {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_medicacao(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Medicação", "pedido_alterar_medicacao"
        )
        assert acc >= 75, f"Medicação {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_comparacao(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Comparação", "comparacao_tratamentos"
        )
        assert acc >= 70, f"Comparação {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_paciente_ativo(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Paciente Ativo", "paciente_ativo"
        )
        assert acc >= 75, f"Paciente Ativo {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_fora_do_escopo(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Fora do Escopo", "fora_do_escopo"
        )
        assert acc >= 70, f"Fora Escopo {c}/{t} = {acc:.0f}%\n" + "\n".join(f)

    def test_fora_do_horario(self, classifier):
        acc, c, t, f = self._run_category(
            classifier, "Fora do Horário", "fora_do_horario"
        )
        assert acc >= 70, f"Fora Horário {c}/{t} = {acc:.0f}%\n" + "\n".join(f)


class TestOverallAccuracy:
    """Test overall accuracy across all 200 cases."""

    def test_overall_accuracy_85_percent(self, classifier):
        correct = 0
        total = len(DATASET)
        failures = []
        for msg, expected in DATASET:
            result = classifier.classify(msg)
            if result.intent == expected:
                correct += 1
            else:
                failures.append(
                    f"  '{msg}' → got '{result.intent}' (conf={result.confidence:.2f}), expected '{expected}'"
                )
        accuracy = correct / total * 100
        # Print summary even on pass
        print(f"\nOverall accuracy: {correct}/{total} = {accuracy:.1f}%")
        if failures:
            print(f"Failures ({len(failures)}):")
            for f in failures[:20]:
                print(f)
            if len(failures) > 20:
                print(f"  ... and {len(failures) - 20} more")
        assert (
            accuracy >= 85
        ), f"Overall accuracy {correct}/{total} = {accuracy:.1f}% < 85%"

    def test_no_false_generics(self, classifier):
        """No message with a specific expected intent should be classified as duvida_generica."""
        false_generics = []
        for msg, expected in DATASET:
            if expected == "duvida_generica_sem_contexto":
                continue
            result = classifier.classify(msg)
            if result.intent == "duvida_generica_sem_contexto":
                false_generics.append(
                    f"  '{msg}' → duvida_generica (expected '{expected}')"
                )
        assert (
            not false_generics
        ), f"False generics ({len(false_generics)}):\n" + "\n".join(false_generics[:10])

    def test_all_confidence_above_95(self, classifier):
        """All 199 cases must produce confidence >= 0.95."""
        low = []
        for msg, expected in DATASET:
            result = classifier.classify(msg)
            if result.confidence < 0.95:
                low.append(f"  [{result.confidence:.1%}] '{msg}' → {result.intent}")
        assert not low, f"Cases below 95% ({len(low)}):\n" + "\n".join(low[:10])


class TestFunnelStageAccuracy:
    """Test that all 199 cases produce the correct funnel stage from LEAD_NOVO."""

    # Expected funnel stage per intent (starting from LEAD_NOVO)
    EXPECTED_STAGE = {
        "saudacao": "lead_novo",
        "interesse_tratamento": "em_qualificacao",
        "pedido_preco": "em_qualificacao",
        "agendar": "em_qualificacao",
        "detalhes_servicos": "lead_novo",
        "disponibilidade_horarios": "lead_novo",
        "informacoes_profissionais": "lead_novo",
        "cultura_historia": "lead_novo",
        "pos_atendimento_duvida": "pos_atendimento",
        "pos_atendimento_sintomas_preocupantes": "pos_atendimento",
        "pedido_alterar_medicacao": "pos_atendimento",
        "reclamacao": "standby",
        "conduta_inadequada_ameaca": "standby",
        "pedido_atendente_humano": "standby",
        "documentos_e_comprovantes": "lead_novo",
        "orcamento_existente": "em_qualificacao",
        "cobranca_renegociacao": "lead_novo",
        "objecao_vou_pensar": "lead_novo",
        "comparacao_tratamentos": "em_qualificacao",
        "paciente_ativo": "paciente_ativo",
        "fora_do_escopo": "lead_novo",
        "fora_do_horario": "lead_novo",
    }

    def test_all_stages_correct(self):
        from uuid import uuid4

        from src.srm.engine import SRMEngine
        from src.srm.models import SRMInput

        engine = SRMEngine()
        wrong = []
        for msg, expected_intent in DATASET:
            inp = SRMInput(
                tenant_id=uuid4(),
                session_id=uuid4(),
                message=msg,
                channel="whatsapp",
            )
            decision = engine.process(inp)
            expected_stage = self.EXPECTED_STAGE.get(expected_intent, "lead_novo")
            if decision.funnel_stage.value != expected_stage:
                wrong.append(
                    f"  '{msg[:50]}' → stage={decision.funnel_stage.value}, "
                    f"expected={expected_stage} (intent={decision.intent})"
                )
        assert not wrong, f"Wrong stages ({len(wrong)}):\n" + "\n".join(wrong[:10])

    def test_escalation_intents_standby(self):
        from uuid import uuid4

        from src.srm.engine import SRMEngine
        from src.srm.models import SRMInput

        engine = SRMEngine()
        escalation_cases = [
            (msg, exp)
            for msg, exp in DATASET
            if exp
            in ("reclamacao", "conduta_inadequada_ameaca", "pedido_atendente_humano")
        ]
        for msg, expected_intent in escalation_cases:
            inp = SRMInput(
                tenant_id=uuid4(),
                session_id=uuid4(),
                message=msg,
                channel="whatsapp",
            )
            decision = engine.process(inp)
            assert (
                decision.funnel_stage.value == "standby"
            ), f"'{msg}' → stage={decision.funnel_stage.value}, expected standby"
            assert (
                decision.safety_concern is True
            ), f"'{msg}' → safety_concern=False, expected True"
            assert (
                decision.handoff_required is True
            ), f"'{msg}' → handoff_required=False, expected True"
