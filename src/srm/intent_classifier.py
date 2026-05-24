"""
NeuroStack - SRM - Intent Classifier
Regex-based intent classification with safety guard clauses.
Uses official intents per documentation MVP v0.5.
"""

import re
from typing import Optional

from src.srm.models import IntentCategory, IntentResult


# ============================================
# Intent Definitions — official intents only
# ============================================

INTENT_DEFINITIONS: dict[str, dict] = {
    # ── Básico ──
    "saudacao": {
        "category": IntentCategory.GREETING,
        "patterns": [
            r"^(ol[aá]|oi|hey|e a[ií]|eai|bom dia|boa tarde|boa noite|al[oô])\b",
            r"^(oi|ol[aá])\s*[!.,]?\s*$",
            r"^(oii+|oi+)\s*[!]?\s*$",
            r"^bom dia\s*[!]?\s*$",
            r"^boa (tarde|noite)\s*[!.,]?\s*(tudo bem)?\s*[?]?\s*$",
        ],
        "keywords": ["olá", "ola", "oi", "bom dia", "boa tarde", "boa noite"],
        "weight": 0.95,
    },

    # ── Vendas e conversão ──
    "interesse_tratamento": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(quero|preciso|gostaria)\b.*\b(tratamento|procedimento|consulta|avaliação|avaliaçao|tratar|fazer|saber)\b",
            r"\b(vocês|voces)\b.*\b(fazem|atendem|têm|tem)\b",
            r"\b(tenho interesse|me interesso)\b",
            r"\b(quero)\b.*\b(tratar|melhorar|cuidar|fazer|entender|saber|marcar)\b",
            r"\b(vocês|voces)\b.*\b(botox|peeling|clareamento|harmonização|harmonizacao|dermatologia|implante)\b",
            r"\b(já fiz|ja fiz|nunca fiz|primeira vez)\b.*\b(botox|procedimento|tratamento|peeling|harmonização|harmonizacao)\b",
            r"\b(tenho|sofro|estou com)\b.*\b(acne|manchas|queda|bruxismo|medo|vergonha|interesse)\b",
            r"\b(posso fazer|posso|podem)\b.*\b(procedimento|tratamento|botox)\b",
            r"\b(experiência ruim|experiencia ruim|medo|vergonha)\b.*\b(dentista|procedimento|tratamento)\b",
            r"\b(quero)\b.*\b(continuar|começar|comecar)\b.*\b(aqui|tratamento)\b",
            r"\b(por onde começo|por onde comeco|como começo|como comeco)\b",
            r"\bnão sei por onde começar\b",
            r"\bquero saber\b",
            r"\bme (explica|conta)\b",
            r"\b(entender melhor)\b.*\b(antes|decidir)\b",
            r"\b(interfere|pode|posso)\b.*\b(tratamento|procedimento|fazer)\b",
            r"\b(grávida|gravida|gestante|diabétic[oa]|diabetico|alérgic[oa]|alergic[oa])\b.*\b(vocês|voces|atendem|posso|pode)\b",
            r"\b(vocês|voces)\b.*\b(grávida|gravida|gestante|diabétic[oa]|diabetico)\b",
            r"\b(idade mínima|idade minima)\b",
            r"\b(ouvi falar|indicaram|recomendaram)\b",
            r"\b(clínica|clinica)\s*x\b",
            r"\b(fiz|faço|faco)\b.*\b(outro lugar|outra clínica|outra clinica)\b",
        ],
        "keywords": ["tratamento", "procedimento", "avaliação", "botox", "peeling",
                      "clareamento", "harmonização", "tratar", "quero fazer",
                      "quero saber", "me explica", "tenho interesse"],
        "weight": 1.0,
    },
    "pedido_preco": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(quanto|qual)\b.*\b(custa|valor|preço|preco|custo)\b",
            r"\b(preço|preco|valor|custo)\b.*\b(consulta|atendimento|sessão|sessao|procedimento)\b",
            r"\b(valores|preços|precos)\b",
            r"\b(parcelam?|parcela|parcelo)\b",
            r"\b(desconto|promoção|promocao)\b",
            r"\b(formas? de pagamento|aceita pix|aceita cartão|aceita cartao)\b",
            r"\b(pacote|combo)\b.*\b(procedimentos?|tratamentos?)\b",
            r"\b(mais caro|mais barato|preço menor|preco menor)\b",
            r"\b(caro|cara)\b.*\b(que|do que)\b.*\b(clínica|clinica)\b",
            r"\bnota fiscal\b",
            r"\bfaixa de preço\b",
            r"\bquanto fica\b",
            r"\bquanto é\b",
            r"\bquanto sai\b",
        ],
        "keywords": ["quanto", "valor", "preço", "preco", "custo", "custa",
                      "parcelam", "desconto", "pagamento", "pix", "pacote"],
        "weight": 1.0,
    },
    "comparacao_tratamentos": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(diferença|diferenca)\b.*\b(entre|tratamento|procedimento)\b",
            r"\b(comparar|comparação|comparacao)\b.*\b(tratamento|procedimento)\b",
            r"\b(qual)\b.*\b(melhor|indicado|mais indicado)\b",
            r"\b(ou)\b.*\b(qual)\b.*\b(melhor|diferença|diferenca)\b",
            r"\b(botox|laser|peeling|implante|prótese|protese|preenchimento|moldeira|clareamento)\b.*\bou\b.*\b(botox|laser|peeling|implante|prótese|protese|preenchimento|moldeira|clareamento)\b",
        ],
        "keywords": ["diferença", "diferenca", "comparar", "melhor", "ou"],
        "weight": 0.9,
    },
    "objecao_vou_pensar": {
        "category": IntentCategory.CONFIRMATION,
        "patterns": [
            # Pensar/decidir
            r"\b(vou)\b.*\b(pensar|ver|decidir)\b",
            r"\b(pensar)\b.*\b(depois|melhor)\b",
            r"\b(te procuro|retorno)\b.*\b(depois)\b",
            r"\b(deixa)\b.*\b(pensar)\b",
            # Preço/caro
            r"\b(achei|muito|tá|ta)\b.*\b(caro|cara)\b",
            r"\b(sem grana|sem dinheiro|não tenho dinheiro|nao tenho dinheiro)\b",
            r"\bvou pesquisar\b",
            # Tempo
            r"\b(não é o momento|nao e o momento|acho que não|acho que nao)\b",
            r"\b(preciso pesquisar|preciso ver|preciso pensar)\b",
            # Confiança/medo
            r"\b(não sei se|nao sei se)\b.*\b(vale|confio|preciso|funciona)\b",
            r"\b(tenho medo|tenho receio)\b",
            r"\b(efeitos? colaterais?)\b",
            r"\b(e se eu não gostar|e se eu nao gostar)\b",
            r"\b(já ouvi|ja ouvi)\b.*\b(dói|doi|ruim)\b",
            # Consulta externa
            r"\b(preciso falar|falar)\b.*\b(marido|esposa|esposo|família|familia)\b",
            r"\b(minha amiga|meu amigo)\b.*\b(disse|falou)\b",
            # Outra clínica
            r"\b(vou ver|ver com)\b.*\b(outra|outro)\b.*\b(clínica|clinica|lugar|profissional)\b",
            # Descrença
            r"\b(já gastei|ja gastei)\b.*\b(não funcionou|nao funcionou)\b",
            r"\b(não estou|nao estou)\b.*\b(convencid[oa]?|cert[oa]|segur[oa])\b",
            r"\b(não tenho certeza|nao tenho certeza)\b",
            r"\b(muito longe)\b",
            r"\b(tenho medo)\b.*\b(procedimento|fazer|cirurgia)\b",
        ],
        "keywords": ["vou pensar", "pensar", "achei caro", "medo", "receio",
                      "pesquisar", "não é o momento", "preciso falar",
                      "não sei se vale", "sem grana"],
        "weight": 1.0,
    },
    "duvida_generica_sem_contexto": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"^(o que|como|quando|onde|por que|qual|quais|quem)\s*[?]?\s*$",
        ],
        "keywords": [],
        "weight": 0.3,
    },

    # ── Agendamento e operação ──
    "agendar": {
        "category": IntentCategory.SCHEDULING,
        "patterns": [
            r"\b(agendar|marcar|reservar)\b.*\b(consulta|horário|horario|atendimento|sessão|sessao|avaliação|avaliaçao|procedimento)\b",
            r"\b(quero|preciso|gostaria)\b.*\b(agendar|marcar)\b",
            r"\b(têm?|há|ha)\b.*\b(horário|horario|vaga|disponibilidade)\b.*\b(disponível|disponivel|livre)?\b",
            r"\b(agenda)\b",  # bare "agenda" is scheduling-specific
            # Date-specific availability asks (without tem/têm): "qual a disponibilidade para segunda?"
            r"\b(disponibilidade)\b.*\b(para|pra|hoje|amanhã|amanha|segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)\b",
            r"\bvamos agendar\b",
            r"\bpode (marcar|agendar)\b",
            r"\b(tem|há)\b.*\b(vaga|encaixe|horário|horario)\b",
            r"\b(pode ser|posso)\b.*\b(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo|manhã|manha|tarde|noite|amanhã|amanha|hoje)\b",
            # Standalone day+time reply (user answering a scheduling question)
            # e.g. "Segunda às 8 da manhã", "Segunda às 8h", "Segunda às 8"
            r"\b(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)\b.*\b(\d{1,2}\s*h|\d{1,2}:\d{2}|manhã|manha|tarde|noite)\b",
            r"\b(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo)\b.*\bàs?\s+\d{1,2}\b",
            r"\b(às?|as)\s+\d{1,2}\s*(h|hora|horas)?\b.*\b(manhã|manha|tarde|noite)\b",
            r"\bquero garantir\b.*\bhorário\b",
            r"\bquando posso ir\b",
            r"\bquero marcar\b(?!.*\b(avaliação|avaliaçao|avaliar)\b)",
            r"\bprimeira consulta\b",
            r"\bmais cedo possível\b",
            r"\b(tem|há)\b.*\b(horário|horario)\b.*\b(manhã|manha|tarde|noite|noturno)\b",
            r"\b(atendem)\b.*\b(fim de semana|sábado|sabado|domingo)\b",
            r"\bcom urgência\b.*\b(agendar|marcar)\b",
            r"\bagendar\b.*\bcom urgência\b",
            r"\bconsigo\b.*\b(horário|horario)\b",
        ],
        "keywords": ["agendar", "marcar", "reservar", "horário", "horario",
                      "consulta", "vaga", "encaixe",
                      "agenda", "disponível", "fim de semana", "sábado",
                      "sabado", "domingo"],
        # NOTE: "disponibilidade" removed from keywords — belongs to disponibilidade_horarios.
        # "tem disponibilidade?" is still captured by pattern line above (tem|há.*disponibilidade).
        "weight": 1.0,
    },
    "reagendar": {
        "category": IntentCategory.SCHEDULING,
        "patterns": [
            r"\b(remarcar|reagendar|alterar)\b.*\b(consulta|horário|horario|data)\b",
            r"\b(mudar|trocar)\b.*\b(horário|horario|dia|data)\b",
            r"\b(preciso|quero|gostaria)\b.*\b(remarcar|reagendar)\b",
        ],
        "keywords": ["remarcar", "reagendar", "alterar", "mudar", "trocar"],
        "weight": 0.9,
    },
    "cancelar": {
        "category": IntentCategory.CANCELLATION,
        "patterns": [
            r"\b(cancelar|desmarcar)\b.*\b(consulta|horário|horario|agendamento)\b",
            r"\b(não|nao)\b.*\b(vou|posso|poderei)\b.*\b(ir|comparecer)\b",
            r"\b(preciso|quero)\b.*\b(cancelar|desmarcar)\b",
            r"\b(desistir)\b.*\b(consulta|procedimento|agendamento)\b",
        ],
        "keywords": ["cancelar", "desmarcar", "desistir"],
        "weight": 1.0,
    },
    "confirmar_presenca": {
        "category": IntentCategory.CONFIRMATION,
        "patterns": [
            r"^(sim|ok|pode|confirmo|confirmado|certo|correto|isso|exato)\s*[!.]?\s*$",
            r"\b(sim|ok)\b.*\b(pode|confirmo)\b",
            r"\b(está|ta|tá)\b.*\b(bom|ótimo|otimo|perfeito|certo)\b",
        ],
        "keywords": ["sim", "ok", "confirmo", "confirmado", "certo", "correto", "perfeito"],
        "weight": 0.9,
    },
    "disponibilidade_horarios": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(horário|horario|hora)\b.*\b(funciona|atende|abre|fecha)\b",
            r"\b(que horas?)\b.*\b(abre|fecha|funciona|atendem)\b",
            r"\b(funcionamento|expediente)\b",
            r"\b(até que horas|ate que horas)\b.*\b(atendem|funciona|abre)\b",
            r"\b(abrem?|fecham?)\b.*\b(sábado|sabado|domingo|feriado)\b",
            # "me informe a disponibilidade" — user asks to be informed about available times
            # NOTE: bare r"\b(disponibilidade)\b" is intentionally omitted — it would beat
            # the agendar "tem disponibilidade?" pattern. Only "informe/diga + disponibilidade"
            # is unambiguously an info query rather than a scheduling request.
            r"\b(informe|informar|diga|fale|me passe)\b.*\b(disponibilidade|horário|horario)\b",
        ],
        "keywords": ["horário", "horario", "funciona", "abre", "fecha",
                      "funcionamento", "expediente"],
        # NOTE: "disponibilidade" intentionally excluded — it scores as agendar for
        # "têm disponibilidade?" and date-specific asks; only "informe+disponibilidade"
        # belongs here (captured by pattern above, not keyword scoring).
        "weight": 1.0,
    },
    "detalhes_servicos": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(quais?|que)\b.*\b(serviços?|procedimentos?|especialidades?)\b",
            r"\b(vocês|voces)\b.*\b(fazem|oferecem|atendem)\b",
            r"\b(onde|qual)\b.*\b(fica|endereço|endereco|localização|localizacao|local)\b",
            r"\b(como)\b.*\b(chegar|chego)\b",
            r"\b(endereço|endereco|localização|localizacao)\b.*\b(clínica|clinica|consultório|consultorio)\b",
            r"\b(aceita|aceitam)\b.*\b(convênio|convenio|plano|seguro)\b",
            r"\b(convênio|convenio|plano)\b.*\b(aceito|atendido)\b",
            r"\b(estacionamento|acessível|acessivel|cadeirante|acompanhante|crianças|criancas|wi-?fi)\b",
            r"\b(particular|convenio|convênio)\b",
            r"\b(tem)\b.*\b(raio.?x|estacionamento|wi.?fi)\b",
            r"\bposso levar\b.*\b(acompanhante)\b",
            r"\b(cirurgia plástica|cirurgia plastica)\b",
        ],
        "keywords": ["serviços", "procedimentos", "especialidades", "endereço", "endereco",
                      "onde fica", "convênio", "convenio", "estacionamento",
                      "acessível", "como chego"],
        "weight": 1.0,
    },
    "pre_atendimento": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(pré|pre)\b.*\b(atendimento|consulta)\b.*\b(preparo|jejum)\b",
        ],
        "keywords": ["pré atendimento", "preparo", "jejum"],
        "weight": 0.85,
    },
    "faq": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(pergunta)\b.*\b(frequente)\b",
            r"\b(dúvida|duvida)\b.*\b(comum)\b",
        ],
        "keywords": ["pergunta frequente", "dúvidas comuns"],
        "weight": 0.8,
    },
    "cultura_historia": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(história|historia)\b.*\b(clínica|clinica)\b",
            r"\b(quem)\b.*\b(são|sao)\b.*\b(vocês|voces)\b",
            r"\b(clínica|clinica)\b.*\b(quantos anos|desde quando)\b",
        ],
        "keywords": ["história", "historia", "quantos anos"],
        "weight": 0.7,
    },
    "qualificacao_publico": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(público|publico)\b.*\b(alvo)\b",
            r"\b(atende)\b.*\b(quem)\b",
        ],
        "keywords": ["público", "publico", "atende quem"],
        "weight": 0.7,
    },
    "informacoes_profissionais": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(médico|medico|doutor|dr|dra)\b.*\b(quem|qual|formação|formacao)\b",
            r"\b(quem)\b.*\b(são|sao)\b.*\b(médicos|medicos|profissionais|doutores)\b",
            # Narrowed: require clinical/professional context to avoid matching "equipe de agendamento"
            r"\b(profissionais|equipe)\b.*\b(clínica|clinica|médica|medica|saúde|saude|dentária|dentaria)\b",
            r"\b(quais?|quem)\b.*\b(profissionais|equipe)\b",
            r"\b(têm|tem|vocês têm|voces tem)\b.*\b(nutricionista|fisioterapeuta|psicólog[oa]|psicologo|dermatologista|ortopedista)\b",
            r"\b(médico|medico)\b.*\b(especialista)\b",
            r"\b(formação|formacao)\b.*\b(dr|dra|doutor|médico|medico)\b",
        ],
        "keywords": ["médico", "medico", "doutor", "profissionais",
                      "especialista", "nutricionista", "formação", "formacao"],
        "weight": 1.1,
    },

    # ── Pós-atendimento ──
    "pos_atendimento_duvida": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(depois|após|apos)\b.*\b(extração|extracao|procedimento|cirurgia|anestesia|aplicação|aplicacao)\b",
            r"\b(posso)\b.*\b(tomar|comer|fazer|dirigir|molhar)\b.*\b(depois|após|apos)?\b",
            r"\b(é normal|e normal)\b.*\b(inchado|inchaço|inchaco|doer|sentir|dormência|dormencia)\b",
            r"\b(quanto tempo)\b.*\b(cicatrizar|durar|efeito|recuperar)\b",
            r"\b(curativo)\b.*\b(pode|devo)\b",
            r"\b(voltar a)\b.*\b(comer|exercício|exercicio|trabalhar|normal)\b",
            r"\b(efeito)\b.*\b(botox|preenchimento)\b.*\b(dura|quanto tempo)\b",
        ],
        "keywords": ["depois do procedimento", "é normal", "posso tomar",
                      "quanto tempo", "cicatrizar", "curativo", "dormência"],
        "weight": 1.0,
    },
    "pos_atendimento_sintomas_preocupantes": {
        "category": IntentCategory.EMERGENCY,
        "patterns": [
            r"\b(dor)\b.*\b(forte|intensa|muita)\b",
            r"\bmuita\b.*\bdor\b",
            r"\b(sangramento|sangrando|sangrar)\b",
            r"\b(febre|piora)\b.*\b(após|apos|depois)\b",
            r"\b(emergência|emergencia|urgente|urgência|urgencia)\b",
            r"\b(passando)\b.*\b(mal)\b",
            r"\b(inchou)\b.*\b(muito|demais)\b",
            r"\b(pus|purulent[oa])\b",
            r"\b(pressão|pressao)\b.*\b(caiu|baixou)\b",
            r"\b(não consigo|nao consigo)\b.*\b(abrir|respirar|engolir)\b",
            r"\b(roxa|roxo|hematoma)\b.*\b(piorando|aumentando)\b",
            r"\b(piorando|piora)\b",
            r"\b(socorro|ajuda)\b",
        ],
        "keywords": ["dor forte", "sangramento", "febre", "piora", "emergência",
                      "passando mal", "inchou", "pus", "pressão caiu", "socorro"],
        "weight": 1.0,
    },
    "pedido_alterar_medicacao": {
        "category": IntentCategory.EMERGENCY,
        "patterns": [
            r"\b(diminuir|aumentar|trocar|parar|suspender)\b.*\b(dose|remédio|remedio|medicamento|medicação|medicacao)\b",
            r"\b(remédio|remedio|medicamento)\b.*\b(forte|fraco)\b",
            r"\b(pode|posso)\b.*\b(tomar)\b.*\b(menos|mais|junto|outro)\b",
            r"\b(posso tomar)\b.*\b(junto|outro medicamento)\b",
            r"\b(efeito colateral)\b.*\b(trocar|mudar)\b",
            r"\b(dose)\b.*\b(fraca|forte)\b",
            r"\b(quero parar)\b.*\b(remédio|remedio|medicamento)\b",
            r"\b(tomar)\b.*\b(menos|mais)\b.*\b(remédio|remedio|medicamento|dose)?\b",
        ],
        "keywords": ["alterar dose", "diminuir remédio", "medicamento forte",
                      "trocar medicamento", "parar de tomar", "efeito colateral",
                      "posso tomar", "dose", "remédio"],
        "weight": 1.1,
    },

    # ── Reclamações e conduta ──
    "reclamacao": {
        "category": IntentCategory.COMPLAINT,
        "patterns": [
            r"\b(reclamar|reclamação|reclamacao|problema|insatisfeit[oa])\b",
            r"\b(péssimo|pessimo|horrível|horrivel|terrível|terrivel)\b",
            r"\b(atendimento|serviço|servico)\b.*\b(péssimo|pessimo|horrível|horrivel|terrível|terrivel|ruim)\b",
            r"\b(péssimo|pessimo|horrível|horrivel|terrível|terrivel|ruim)\b.*\b(atendimento|serviço|servico)\b",
            r"\b(foi)\b.*\b(péssimo|pessimo|horrível|horrivel|terrível|terrivel|ruim)\b",
            r"\b(não gostei|nao gostei)\b.*\b(resultado|atendimento|serviço|servico)\b",
            r"\b(quero)\b.*\b(dinheiro|reembolso|estorno)\b.*\b(volta|devolver|devolvido)\b",
            r"\b(dinheiro)\b.*\b(volta|de volta|devolver)\b",
            r"\b(reembolso|estorno|devolver|devolução|devolvam)\b",
            r"\b(arrependid[oa]|decepciona[dr])\b",
            r"\b(quero|preciso)\b.*\b(falar|conversar)\b.*\b(gerente|responsável|responsavel|supervisor)\b",
            r"\b(demora|demorado|esperando|espera)\b",
            r"\b(ninguém|ninguem)\b.*\b(respondeu|atende|responde)\b",
            r"\b(mal atendid[oa])\b",
            r"\b(médico|medico|doutor)\b.*\b(atrasou)\b",
            r"\b(não resolveram|nao resolveram)\b",
            r"\b(já liguei|ja liguei)\b.*\b(vezes)\b",
            r"\b(resultado)\b.*\b(não ficou|nao ficou|diferente|prometido)\b",
            r"\b(muito insatisfeit[oa])\b",
            r"\b(esperando há|esperando ha)\b",
        ],
        "keywords": ["reclamar", "reclamação", "reclamacao", "problema", "insatisfeito",
                      "péssimo", "pessimo", "demora", "esperando", "mal atendida",
                      "não resolveram", "não gostei", "nao gostei", "dinheiro de volta",
                      "reembolso", "estorno", "decepcionado", "arrependido"],
        "weight": 1.0,
    },
    "conduta_inadequada_ameaca": {
        "category": IntentCategory.COMPLAINT,
        "patterns": [
            r"\b(processar|processo)\b",
            r"\b(advogado|procon|crm|juiz)\b",
            r"\b(absurdo|vergonha)\b.*\b(vou)\b",
            r"\b(vou)\b.*\b(processar|denunciar|expor)\b",
            r"\b(ameaça|ameaca)\b",
            r"\b(merda|porcaria|lixo)\b",
            r"\b(vão se arrepender|vao se arrepender)\b",
            r"\b(expor)\b.*\b(redes|internet|instagram)\b",
        ],
        "keywords": ["processar", "advogado", "procon", "crm", "absurdo",
                      "ameaça", "ameaca", "merda", "denunciar"],
        "weight": 1.0,
    },

    # ── Documentos e financeiro ──
    "documentos_e_comprovantes": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(documento|comprovante|atestado|receita|laudo|nota fiscal)\b",
            r"\b(envia|manda|enviar|mandar)\b.*\b(documento|receita|laudo|atestado|nota|comprovante)\b",
            r"\b(preciso|quero)\b.*\b(atestado|comprovante|receita|laudo|nota fiscal)\b",
            r"\b(comprovante)\b.*\b(comparecimento)\b",
            r"\bnota fiscal\b.*\b(procedimento|consulta|atendimento)\b",
        ],
        "keywords": ["documento", "comprovante", "atestado", "receita",
                      "laudo", "nota fiscal"],
        "weight": 1.1,
    },
    "orcamento_existente": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(orçamento|orcamento)\b.*\b(anterior|que fiz|que me passaram|existente)\b",
            r"\b(já recebi|ja recebi)\b.*\b(orçamento|orcamento)\b",
            r"\b(quero ver)\b.*\b(orçamento|orcamento)\b",
        ],
        "keywords": ["orçamento", "orcamento", "orçamento anterior"],
        "weight": 0.9,
    },
    "cobranca_renegociacao": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(parcelar|parcela)\b.*\b(débito|debito|dívida|divida)\b",
            r"\b(renegociar|negociar)\b.*\b(cobrança|cobranca|dívida|divida)\b",
            r"\b(recebi)\b.*\b(cobrança|cobranca)\b",
            r"\b(quero parcelar)\b.*\b(débito|debito)\b",
            r"\b(dívida|divida)\b.*\b(renegociar|parcelar)\b",
        ],
        "keywords": ["parcelar débito", "renegociar", "cobrança", "cobranca", "dívida", "divida"],
        "weight": 0.9,
    },

    # ── New intents from 200-case dataset ──
    "pedido_atendente_humano": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(falar|conversar|atendimento)\b.*\b(humano|pessoa|atendente|real|verdade)\b",
            r"\b(quero|preciso)\b.*\b(falar|conversar)\b.*\b(alguém|alguem|pessoa|humano)\b",
            r"\b(não|nao)\b.*\b(robô|robo|bot|máquina|maquina)\b",
            r"\b(me transfere|transferir)\b.*\b(alguém|alguem|pessoa|atendente)\b",
            r"\b(tem como falar)\b.*\b(pessoa|humano|alguém|alguem)\b",
            # "encaminhar pra equipe / atendente / agendamento" — user asking for handoff
            r"\b(encaminh(ar|e|a|asse|em))\b.*\b(equipe|atendente|pessoa|agendamento|secretaria)\b",
            r"\b(gostaria|quero|preciso)\b.*\b(encaminh)\b",
            r"\bme encaminh(e|ar|em)\b",
        ],
        "keywords": ["atendente humano", "humano", "pessoa real", "não robô",
                      "falar com alguém", "transfere"],
        # NOTE: do NOT add bare verbs like "encaminhar"/"encaminhe" here —
        # they are too generic and cause false-positives ("encaminhar exames").
        # The patterns above already require handoff context (equipe/agendamento/etc.)
        "weight": 1.0,
    },
    "paciente_ativo": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(já sou|ja sou)\b.*\b(paciente|cliente)\b",
            r"\b(faço|faco)\b.*\b(acompanhamento|tratamento)\b.*\b(há|ha|aqui|na clínica|na clinica)\b",
            r"\b(sou paciente)\b.*\b(dr|dra|doutor|clínica|clinica)\b",
            r"\b(já faço|ja faco)\b.*\b(tratamento)\b.*\b(aqui)\b",
        ],
        "keywords": ["já sou paciente", "faço acompanhamento", "sou paciente",
                      "já faço tratamento"],
        "weight": 0.9,
    },
    "fora_do_escopo": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(vocês|voces)\b.*\b(tratam|fazem|atendem|vendem|têm|tem)\b.*\b(depressão|depressao|veterinário|veterinario|animais|vacinação|vacinacao|ressonância|ressonancia)\b",
            r"\b(fazem|tem)\b.*\b(exame de sangue|exame de vista|ressonância|ressonancia|vacinação|vacinacao)\b",
            r"\b(atendem)\b.*\b(veterinário|veterinario)\b",
            r"\b(vendem)\b.*\b(remédio|remedio)\b",
            r"\b(laboratório|laboratorio)\b.*\b(análises|analises)\b",
            r"\b(cirurgia bariátrica|cirurgia bariatrica)\b",
            r"\b(tratam)\b.*\b(animais)\b",
            r"\b(posso fazer)\b.*\b(ressonância|ressonancia)\b",
        ],
        "keywords": ["exame de sangue", "exame de vista", "veterinário", "veterinario",
                      "vendem remédio", "laboratório", "laboratorio", "vacinação", "vacinacao",
                      "ressonância", "ressonancia", "bariátrica", "bariatrica", "animais"],
        "weight": 1.1,
    },
    "fora_do_horario": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(fora do horário|fora do horario)\b",
            r"\b(madrugada)\b.*\b(abrem|vocês|voces)\b",
            r"\b(plantão|plantao)\b",
            r"\b(atendimento)\b.*\b(fora)\b.*\b(horário|horario)\b",
            r"\b(emergência|emergencia)\b.*\b(fora)\b.*\b(horário|horario)\b",
            r"\b(vocês|voces)\b.*\b(plantão|plantao)\b",
        ],
        "keywords": ["fora do horário", "fora do horario", "madrugada", "plantão", "plantao"],
        "weight": 1.1,
    },
}


# ============================================
# English Intent Patterns
# ============================================
# Mirrors INTENT_DEFINITIONS but with English regex/keywords.
# When language="en", these patterns override the PT ones per intent;
# any intent missing here falls back to its PT patterns.

EN_INTENT_PATTERNS: dict[str, dict] = {
    "saudacao": {
        "patterns": [
            r"^(hello|hi+|hey+|howdy|yo)\b[!.,?]*\s*$",
            r"^(hello|hi|hey)\b.*$",
            r"\bgood (morning|afternoon|evening|night)\b",
        ],
        "keywords": ["hello", "hi", "hey", "howdy", "good morning",
                      "good afternoon", "good evening"],
    },
    "interesse_tratamento": {
        "patterns": [
            r"\b(i'?m|i am)\s+interested\s+in\b",
            r"\b(i want|i'd like|i would like|i need)\b.*\b(to know about|information about|treatment|procedure|consultation)\b",
            r"\btell me (more )?about\b",
            r"\b(i'?m|i am) looking for\b.*\b(treatment|procedure|clinic)\b",
            r"\b(do you|can you)\b.*\b(treat|offer|do|perform)\b",
            r"\bi want to know\b",
        ],
        "keywords": ["interested in", "tell me about", "looking for treatment",
                      "i want to know", "information about"],
    },
    "pedido_preco": {
        "patterns": [
            r"\bhow much\b",
            r"\bwhat'?s the (price|cost|fee|rate)\b",
            r"\b(price|cost|fee|fees|rate|pricing|quote)\b",
            r"\bdoes it cost\b",
            r"\bhow much (is|does|do)\b",
            r"\bcan i pay\b.*\b(installments|in parts)\b",
            r"\b(discount|promotion|offer)\b",
            r"\bpayment (method|options?)\b",
        ],
        "keywords": ["how much", "price", "cost", "pricing", "fee", "fees",
                      "rate", "quote", "value", "payment"],
    },
    "comparacao_tratamentos": {
        "patterns": [
            r"\bcompared to\b",
            r"\b(versus|vs\.?)\b",
            r"\bdifference between\b",
            r"\bbetter than\b",
            r"\bwhich (is|one is) better\b",
            r"\b(which|what) (do you|would you) recommend\b",
        ],
        "keywords": ["compared to", "versus", "difference between",
                      "better than", "which is better"],
    },
    "objecao_vou_pensar": {
        "patterns": [
            r"\bi'?ll think\b",
            r"\blet me think\b",
            r"\bneed to think\b",
            r"\bwill consider\b",
            r"\bmaybe later\b",
            r"\bnot sure( yet)?\b",
            r"\bit'?s (too )?expensive\b",
            r"\bi'?m afraid\b",
            r"\bside effects?\b",
            r"\bi'?ll get back to you\b",
        ],
        "keywords": ["think about it", "let me think", "maybe later",
                      "not sure", "too expensive", "afraid"],
    },
    "duvida_generica_sem_contexto": {
        "patterns": [
            r"^(what|how|when|where|why|which|who)\s*[?]?\s*$",
            r"\bcan you explain\b",
            r"\bi have a question\b",
            r"\btell me more\b",
        ],
        "keywords": [],
    },
    "agendar": {
        "patterns": [
            r"\b(i want|i'd like|i would like|i need)\b.*\b(to )?(schedule|book|make|set up)\b",
            r"\b(schedule|book|reserve)\b.*\b(appointment|consultation|visit|session)\b",
            r"\bmake an appointment\b",
            r"\bcan i (schedule|book|come in)\b",
            r"\bi want to book\b",
            r"\bset up (a |an )?(meeting|appointment)\b",
            r"\bavailable (slots?|times?|appointments?)\b",
        ],
        "keywords": ["schedule", "book", "appointment", "reserve",
                      "make an appointment", "set up"],
    },
    "reagendar": {
        "patterns": [
            r"\breschedule\b",
            r"\b(change|move)\b.*\b(my )?appointment\b",
            r"\bmove (the |my )?appointment\b",
            r"\bcan we (change|move|reschedule)\b",
        ],
        "keywords": ["reschedule", "change my appointment", "move appointment"],
    },
    "cancelar": {
        "patterns": [
            r"\bcancel\b.*\b(appointment|consultation|booking|session)?\b",
            r"\bi want to cancel\b",
            r"\bi (can'?t|cannot|won'?t)\b.*\b(make it|come|attend)\b",
        ],
        "keywords": ["cancel", "cancel appointment", "can't make it"],
    },
    "confirmar_presenca": {
        "patterns": [
            r"^(yes|ok|okay|sure|confirmed|confirm|correct|right|exactly)\b[!.]?\s*$",
            r"\bi'?ll be there\b",
            r"\bconfirming (my )?appointment\b",
            r"\byes,? i'?m coming\b",
        ],
        "keywords": ["yes", "ok", "confirm", "i'll be there", "coming"],
    },
    "disponibilidade_horarios": {
        "patterns": [
            r"\bavailable (times?|hours?|slots?)\b",
            r"\bwhen are you open\b",
            r"\bwhat (times?|hours?) (are you|do you) open\b",
            r"\bwhat times? (are )?available\b",
            r"\b(business|opening|operating) hours\b",
            r"\bschedule availability\b",
        ],
        "keywords": ["available times", "when are you open",
                      "business hours", "opening hours"],
    },
    "detalhes_servicos": {
        "patterns": [
            r"\bwhat services\b",
            r"\bwhat (do you|services do you) offer\b",
            r"\bwhat procedures\b",
            r"\bservice details\b",
            r"\bwhere (are you|is the clinic) located\b",
            r"\bwhat'?s your address\b",
            r"\b(do|does) (you|the clinic) accept\b.*\b(insurance|plan)\b",
            r"\bparking\b",
        ],
        "keywords": ["what services", "what do you offer", "procedures",
                      "address", "location", "insurance", "parking"],
    },
    "pre_atendimento": {
        "patterns": [
            r"\bbefore (the |my )?(appointment|procedure|consultation)\b",
            r"\bwhat (should i|do i need to|to) (bring|do|prepare)\b",
            r"\b(preparation|prep|fasting)\b.*\b(appointment|procedure)?\b",
        ],
        "keywords": ["before the appointment", "what to bring",
                      "preparation", "fasting"],
    },
    "faq": {
        "patterns": [
            r"\bfrequently asked\b",
            r"\bcommon questions?\b",
            r"\bhow does (it|this) work\b",
        ],
        "keywords": ["frequently asked", "common questions", "how does it work"],
    },
    "cultura_historia": {
        "patterns": [
            r"\babout the clinic\b",
            r"\bclinic('?s)? history\b",
            r"\bwho are you\b",
            r"\babout you(r clinic)?\b",
            r"\bhow long have you\b",
        ],
        "keywords": ["about the clinic", "clinic history", "who are you"],
    },
    "qualificacao_publico": {
        "patterns": [
            r"\bis this for me\b",
            r"\b(do|does) you treat\b",
            r"\bwho is this for\b",
            r"\bam i a (candidate|good fit)\b",
        ],
        "keywords": ["is this for me", "do you treat", "candidate"],
    },
    "informacoes_profissionais": {
        "patterns": [
            r"\b(doctor|dr\.?|physician|specialist)\b.*\b(who|what|qualifications|formation)\b",
            r"\bwho will (treat|see) me\b",
            r"\b(your |the )?(staff|team|professionals)\b",
            r"\b(do you have|is there)\b.*\b(specialist|dermatologist|psychologist|nutritionist)\b",
        ],
        "keywords": ["doctor", "professional", "specialist", "staff", "team",
                      "who will treat me"],
    },
    "pos_atendimento_duvida": {
        "patterns": [
            r"\bafter (the |my )?(procedure|appointment|treatment|surgery)\b",
            r"\bpost(-|\s)?(treatment|procedure|op)\b",
            r"\bfollow(-|\s)?up question\b",
            r"\baftercare\b",
            r"\bcan i (eat|drink|drive|exercise) after\b",
            r"\bhow long (until|before|will it take)\b",
        ],
        "keywords": ["after the procedure", "post treatment", "follow up",
                      "aftercare"],
    },
    "pos_atendimento_sintomas_preocupantes": {
        "patterns": [
            r"\b(severe|bad|terrible) pain\b",
            r"\b(bleeding|swelling|fever)\b",
            r"\ballergic reaction\b",
            r"\bside effects?\b",
            r"\bsomething('?s| is) wrong\b",
            r"\b(emergency|urgent)\b",
            r"\bgetting worse\b",
        ],
        "keywords": ["pain", "swelling", "bleeding", "allergic reaction",
                      "side effect", "something wrong", "emergency"],
    },
    "pedido_alterar_medicacao": {
        "patterns": [
            r"\b(change|alter|adjust|stop)\b.*\b(my )?(medication|prescription|meds|dose|dosage)\b",
            r"\bcan i (take|stop)\b.*\b(medication|meds|pill)\b",
            r"\b(lower|higher|stronger|weaker)\b.*\b(dose|dosage)\b",
        ],
        "keywords": ["change medication", "alter prescription",
                      "adjust meds", "stop taking", "dose"],
    },
    "reclamacao": {
        "patterns": [
            r"\bcomplaint\b",
            r"\b(i'?m |i am )?(not satisfied|unhappy|dissatisfied|disappointed)\b",
            r"\b(terrible|awful|horrible|bad)\b.*\b(service|treatment|experience)\b",
            r"\b(service|treatment|experience)\b.*\b(was|is)\b.*\b(terrible|awful|horrible|bad)\b",
            r"\b(i )?(want|need|demand)\b.*\b(refund|my money back)\b",
            r"\bmoney back\b",
            r"\bi hated?\b",
            r"\bbad service\b",
            r"\bspeak to (a |the )?(manager|supervisor)\b",
        ],
        "keywords": ["complaint", "not satisfied", "unhappy", "terrible",
                      "awful", "refund", "money back", "disappointed",
                      "bad service", "hated"],
    },
    "conduta_inadequada_ameaca": {
        "patterns": [
            r"\b(sue|lawsuit|lawyer|attorney)\b",
            r"\bi'?ll (sue|report)\b",
            r"\breport you\b",
            r"\bthis is (unacceptable|outrageous|absurd)\b",
            r"\b(threat|threaten)\b",
            r"\b(abusive|abuse)\b",
            r"\bexpose you\b.*\b(online|social media)\b",
        ],
        "keywords": ["sue", "lawyer", "report you", "unacceptable",
                      "threat", "abusive"],
    },
    "documentos_e_comprovantes": {
        "patterns": [
            r"\b(receipt|invoice|document|certificate)\b",
            r"\bproof of (service|attendance|payment)\b",
            r"\b(send|email|provide)\b.*\b(receipt|invoice|document|certificate)\b",
            r"\bmedical (record|report)\b",
        ],
        "keywords": ["receipt", "invoice", "document", "certificate",
                      "proof of service", "medical record"],
    },
    "orcamento_existente": {
        "patterns": [
            r"\bi (have|got)\b.*\b(quote|estimate|budget)\b",
            r"\bmy (budget|quote) is\b",
            r"\bprevious (quote|estimate)\b",
        ],
        "keywords": ["i have a quote", "my budget", "previous quote"],
    },
    "cobranca_renegociacao": {
        "patterns": [
            r"\b(billing|payment) (issue|problem|question)\b",
            r"\b(charge|charged|charging)\b.*\b(wrong|dispute|incorrect)\b",
            r"\b(renegotiate|negotiate)\b.*\b(debt|payment|bill)\b",
            r"\bdispute (the |a )?(charge|bill)\b",
            r"\binstallments?\b.*\b(debt|payment|bill)\b",
        ],
        "keywords": ["billing", "payment issue", "renegotiate",
                      "dispute charge", "wrong charge"],
    },
    "pedido_atendente_humano": {
        "patterns": [
            r"\b(talk|speak|chat)\b.*\b(to|with)\b.*\b(human|person|real person|agent|attendant|someone)\b",
            r"\bi want\b.*\b(human|real person|person)\b",
            r"\b(not|don'?t want)\b.*\b(bot|robot|ai|machine)\b",
            r"\btransfer me\b.*\b(human|person|someone|agent)\b",
            r"\bcan i (talk|speak) to (a )?(human|person|someone)\b",
        ],
        "keywords": ["human", "real person", "speak to someone",
                      "talk to a person", "agent", "attendant", "not a bot"],
    },
    "paciente_ativo": {
        "patterns": [
            r"\bi'?m (a |an )?(patient|client)\b",
            r"\bi already (use|go|come)\b",
            r"\b(returning|existing) patient\b",
            r"\bi'?ve been (here |to you )?before\b",
            r"\bi already (have|am) (a )?(patient|treatment)\b",
        ],
        "keywords": ["i'm a patient", "returning patient",
                      "already use", "been before"],
    },
    "fora_do_escopo": {
        "patterns": [
            r"\b(not related|off topic|different topic|something else)\b",
            r"\bdo you (sell|do)\b.*\b(veterinary|animals|pets|blood test|x-ray)\b",
            r"\b(unrelated|nothing to do)\b",
        ],
        "keywords": ["not related", "off topic", "different topic",
                      "something else"],
    },
    "fora_do_horario": {
        "patterns": [
            r"\bare you open\b",
            r"\b(are you )?closed\b",
            r"\b(business|operating|opening) hours\b",
            r"\bwhat time do you (open|close)\b",
            r"\bafter hours\b",
        ],
        "keywords": ["are you open", "closed", "business hours",
                      "what time do you open", "after hours"],
    },
}


# ============================================
# Safety Guard Patterns (checked before scoring)
# ============================================

_SAFETY_GUARD_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(dor)\b.*\b(forte|intensa|muita|não passa|nao passa)\b", re.IGNORECASE),
    re.compile(r"\bmuita\b.*\bdor\b", re.IGNORECASE),
    re.compile(r"\b(sangramento|sangrando|sangrar)\b", re.IGNORECASE),
    re.compile(r"\bo sangramento não para\b", re.IGNORECASE),
    re.compile(r"\b(febre)\b.*\b(após|apos|depois|procedimento|cirurgia)\b", re.IGNORECASE),
    re.compile(r"\b(inchou)\b.*\b(muito|demais)\b", re.IGNORECASE),
    re.compile(r"\bpiorando\b", re.IGNORECASE),
    re.compile(r"\b(passando)\b.*\b(mal)\b.*\b(depois|após|apos|procedimento)?\b", re.IGNORECASE),
    re.compile(r"\b(pus|purulent)\b", re.IGNORECASE),
    re.compile(r"\bsaiu\b.*\bpus\b", re.IGNORECASE),
    re.compile(r"\b(pressão|pressao)\b.*\b(caiu|baixou)\b", re.IGNORECASE),
    re.compile(r"\b(não consigo|nao consigo)\b.*\b(abrir|respirar|engolir)\b", re.IGNORECASE),
    re.compile(r"\b(rox[ao]|hematoma)\b.*\b(piorando|aumentando)\b", re.IGNORECASE),
    re.compile(r"\bdor\b.*\bintens[ao]\b", re.IGNORECASE),
]


class IntentClassifier:
    """
    Regex-based intent classifier with safety guard clauses.
    """

    def __init__(
        self,
        custom_intents: Optional[dict] = None,
    ):
        self.intents = INTENT_DEFINITIONS.copy()
        if custom_intents:
            self.intents.update(custom_intents)

        # Compile regex patterns (PT — default)
        self._compiled_patterns: dict[str, list[re.Pattern]] = {}
        for intent_name, intent_def in self.intents.items():
            self._compiled_patterns[intent_name] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in intent_def.get("patterns", [])
            ]

        # Compile English patterns (only those defined in EN_INTENT_PATTERNS)
        self._compiled_patterns_en: dict[str, list[re.Pattern]] = {}
        self._en_keywords: dict[str, list[str]] = {}
        for intent_name, en_def in EN_INTENT_PATTERNS.items():
            self._compiled_patterns_en[intent_name] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in en_def.get("patterns", [])
            ]
            self._en_keywords[intent_name] = en_def.get("keywords", [])

    def classify(self, message: str, language: str = "pt-BR") -> IntentResult:
        """
        Classify the intent of a message.

        Strategy:
        1. Safety guard — check for safety-critical keywords, return immediately
        2. Regex scoring — score all intents, pick best match
        """
        message_lower = message.strip().lower()

        if not message_lower:
            return IntentResult(
                intent="duvida_generica_sem_contexto",
                category=IntentCategory.UNKNOWN,
                confidence=0.0,
                matched_patterns=[],
            )

        # Safety guard: check for safety-critical keywords FIRST
        for pattern in _SAFETY_GUARD_PATTERNS:
            if pattern.search(message_lower):
                return IntentResult(
                    intent="pos_atendimento_sintomas_preocupantes",
                    category=IntentCategory.EMERGENCY,
                    confidence=1.0,
                    matched_patterns=[pattern.pattern],
                )

        # Regex classification
        return self._classify_with_regex(message_lower, language=language)

    def _classify_with_regex(self, message: str, language: str = "pt-BR") -> IntentResult:
        """Classify using regex patterns."""
        best_intent = "duvida_generica_sem_contexto"
        best_category = IntentCategory.QUESTION
        best_score = 0.0
        second_best_score = 0.0
        matched_patterns: list[str] = []

        for intent_name, intent_def in self.intents.items():
            score, patterns = self._score_intent(message, intent_name, intent_def, language=language)

            if score > best_score:
                second_best_score = best_score
                best_score = score
                best_intent = intent_name
                best_category = intent_def["category"]
                matched_patterns = patterns
            elif score > second_best_score:
                second_best_score = score

        # Confidence normalization: a regex match that wins the scoring is high confidence.
        # Any positive score that beats all alternatives gets floored at 0.95.
        confidence = min(best_score, 1.0)
        if best_score > 0 and best_score > second_best_score:
            confidence = max(confidence, 0.95)

        return IntentResult(
            intent=best_intent,
            category=best_category,
            confidence=round(confidence, 3),
            matched_patterns=matched_patterns,
        )

    def _score_intent(
        self,
        message: str,
        intent_name: str,
        intent_def: dict,
        language: str = "pt-BR",
    ) -> tuple[float, list[str]]:
        """Calculate score for an intent based on pattern and keyword matching."""
        score = 0.0
        matched: list[str] = []
        weight = intent_def.get("weight", 1.0)

        # Select pattern + keyword set by language.
        # EN patterns override PT for any intent defined in EN_INTENT_PATTERNS;
        # intents missing from EN fall back to PT patterns.
        use_en = language.lower().startswith("en") and intent_name in self._compiled_patterns_en
        if use_en:
            patterns = self._compiled_patterns_en.get(intent_name, [])
            keywords = self._en_keywords.get(intent_name, [])
        else:
            patterns = self._compiled_patterns.get(intent_name, [])
            keywords = intent_def.get("keywords", [])

        # Pattern matching (higher weight)
        for pattern in patterns:
            if pattern.search(message):
                score += 0.5 * weight
                matched.append(pattern.pattern)

        # Keyword matching (lower weight)
        keyword_matches = 0
        for keyword in keywords:
            if keyword.lower() in message:
                keyword_matches += 1

        if keywords:
            keyword_score = (keyword_matches / len(keywords)) * 0.4 * weight
            score += keyword_score

        # Bonus for multiple pattern matches
        if len(matched) > 1:
            score += 0.1 * len(matched)

        return score, matched

    def get_category_intents(self, category: IntentCategory) -> list[str]:
        """Get all intents for a given category."""
        return [
            name for name, def_ in self.intents.items()
            if def_["category"] == category
        ]

    def add_custom_intent(
        self,
        name: str,
        category: IntentCategory,
        patterns: list[str],
        keywords: list[str],
        weight: float = 1.0,
    ) -> None:
        """Add a custom intent definition."""
        self.intents[name] = {
            "category": category,
            "patterns": patterns,
            "keywords": keywords,
            "weight": weight,
        }
        self._compiled_patterns[name] = [
            re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns
        ]


# ============================================
# Singleton Instance
# ============================================

_classifier: Optional[IntentClassifier] = None


def get_intent_classifier() -> IntentClassifier:
    """Get or create the global IntentClassifier instance."""
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier
