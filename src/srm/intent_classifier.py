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
            r"\b(tem|há|ha)\b.*\b(horário|horario|vaga|disponibilidade)\b.*\b(disponível|disponivel|livre)?\b",
            r"\b(disponibilidade|agenda)\b",
            r"\bvamos agendar\b",
            r"\bpode (marcar|agendar)\b",
            r"\b(tem|há)\b.*\b(vaga|encaixe|horário|horario)\b",
            r"\b(pode ser|posso)\b.*\b(segunda|terça|terca|quarta|quinta|sexta|sábado|sabado|domingo|manhã|manha|tarde|noite|amanhã|amanha|hoje)\b",
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
                      "consulta", "vaga", "disponibilidade", "encaixe",
                      "agenda", "disponível", "fim de semana", "sábado",
                      "sabado", "domingo"],
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
        ],
        "keywords": ["horário", "horario", "funciona", "abre", "fecha",
                      "funcionamento", "expediente"],
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
            r"\b(profissionais|equipe)\b",
            r"\b(têm|tem|vocês têm|voces tem)\b.*\b(nutricionista|fisioterapeuta|psicólog[oa]|psicologo|dermatologista|ortopedista)\b",
            r"\b(médico|medico)\b.*\b(especialista)\b",
            r"\b(formação|formacao)\b.*\b(dr|dra|doutor|médico|medico)\b",
        ],
        "keywords": ["médico", "medico", "doutor", "profissionais", "equipe",
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
        ],
        "keywords": ["atendente humano", "humano", "pessoa real", "não robô",
                      "falar com alguém", "transfere"],
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

        # Compile regex patterns
        self._compiled_patterns: dict[str, list[re.Pattern]] = {}
        for intent_name, intent_def in self.intents.items():
            self._compiled_patterns[intent_name] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in intent_def.get("patterns", [])
            ]

    def classify(self, message: str) -> IntentResult:
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
        return self._classify_with_regex(message_lower)

    def _classify_with_regex(self, message: str) -> IntentResult:
        """Classify using regex patterns."""
        best_intent = "duvida_generica_sem_contexto"
        best_category = IntentCategory.QUESTION
        best_score = 0.0
        second_best_score = 0.0
        matched_patterns: list[str] = []

        for intent_name, intent_def in self.intents.items():
            score, patterns = self._score_intent(message, intent_name, intent_def)

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
    ) -> tuple[float, list[str]]:
        """Calculate score for an intent based on pattern and keyword matching."""
        score = 0.0
        matched: list[str] = []
        weight = intent_def.get("weight", 1.0)

        # Pattern matching (higher weight)
        patterns = self._compiled_patterns.get(intent_name, [])
        for pattern in patterns:
            if pattern.search(message):
                score += 0.5 * weight
                matched.append(pattern.pattern)

        # Keyword matching (lower weight)
        keywords = intent_def.get("keywords", [])
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
