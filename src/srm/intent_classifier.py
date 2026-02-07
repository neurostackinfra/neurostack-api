"""
NeuroStack - SRM - Intent Classifier
Hybrid intent classification using Naive Bayes + Regex patterns.
Uses 23 official intents (CTX/SRM Intents v0.1) per documentation MVP v0.5.
"""

import re
from typing import Optional

from src.srm.models import IntentCategory, IntentResult
from src.srm.intent_naive_bayes import IntentNaiveBayes


# Map legacy intent names (from regex/bayes) to official 23 intents for backward compat
LEGACY_TO_OFFICIAL_INTENT: dict[str, str] = {
    "greeting_hello": "saudacao",
    "question_price": "pedido_preco",
    "scheduling_request": "agendar",
    "scheduling_reschedule": "reagendar",
    "cancellation_request": "cancelar",
    "confirmation_positive": "confirmar_presenca",
    "confirmation_negative": "confirmar_presenca",
    "question_hours": "disponibilidade_horarios",
    "question_services": "detalhes_servicos",
    "question_location": "detalhes_servicos",
    "question_insurance": "detalhes_servicos",
    "question_general": "duvida_generica_sem_contexto",
    "complaint_general": "reclamacao",
    "complaint_wait": "reclamacao",
    "emergency_medical": "pos_atendimento_sintomas_preocupantes",
    "handoff_request": "duvida_generica_sem_contexto",
    "information_provide": "duvida_generica_sem_contexto",
    "feedback_positive": "duvida_generica_sem_contexto",
    "feedback_negative": "reclamacao",
    "farewell_bye": "duvida_generica_sem_contexto",
    "unknown": "duvida_generica_sem_contexto",
}


# ============================================
# Intent Definitions (23 official + legacy for pattern matching)
# ============================================

# Primary: 23 official intents. Legacy keys kept for pattern reuse.
INTENT_DEFINITIONS: dict[str, dict] = {
    # Básico
    "saudacao": {
        "category": IntentCategory.GREETING,
        "patterns": [
            r"^(ol\u00e1|ola|oi|hey|e a\u00ed|eai|bom dia|boa tarde|boa noite|al\u00f4|alo)\b",
            r"^(oi|ol\u00e1|ola)\s*[!.,]?$",
        ],
        "keywords": ["ol\u00e1", "ola", "oi", "bom dia", "boa tarde", "boa noite"],
        "weight": 0.95,
    },
    # Vendas e conversão
    "interesse_tratamento": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(quero|preciso|gostaria)\b.*\b(tratamento|procedimento|consulta|avalia\u00e7\u00e3o)\b",
            r"\b(quais?|que)\b.*\b(servi\u00e7os?|procedimentos?|especialidades?)\b",
        ],
        "keywords": ["tratamento", "procedimento", "avalia\u00e7\u00e3o", "servi\u00e7os"],
        "weight": 1.0,
    },
    "pedido_preco": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(quanto|qual)\b.*\b(custa|valor|pre\u00e7o|custo)\b",
            r"\b(pre\u00e7o|valor|custo)\b.*\b(consulta|atendimento|sess\u00e3o)\b",
            r"\b(valores|pre\u00e7os)\b",
        ],
        "keywords": ["quanto", "valor", "pre\u00e7o", "custo", "custa"],
        "weight": 1.0,
    },
    "comparacao_tratamentos": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(diferen\u00e7a|comparar|comparacao)\b.*\b(tratamento|procedimento)\b",
            r"\b(qual)\b.*\b(melhor|indicado)\b",
        ],
        "keywords": ["comparar", "diferen\u00e7a", "melhor"],
        "weight": 0.9,
    },
    "objecao_vou_pensar": {
        "category": IntentCategory.CONFIRMATION,
        "patterns": [
            r"\b(vou)\b.*\b(pensar|ver|decidir)\b",
            r"\b(pensar)\b.*\b(depois|melhor)\b",
            r"\b(te procuro|retorno)\b.*\b(depois)\b",
            r"\b(deixa)\b.*\b(pensar)\b",
        ],
        "keywords": ["vou pensar", "pensar", "depois te procuro", "deixa eu pensar"],
        "weight": 1.0,
    },
    "duvida_generica_sem_contexto": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"^(o que|como|quando|onde|por que|qual|quais|quem)\b",
            r"\?$",
        ],
        "keywords": ["o que", "como", "quando", "onde", "qual", "quem"],
        "weight": 0.6,
    },
    # Agendamento e operação
    "agendar": {
        "category": IntentCategory.SCHEDULING,
        "patterns": [
            r"\b(agendar|marcar|reservar)\b.*\b(consulta|hor\u00e1rio|atendimento|sess\u00e3o)\b",
            r"\b(quero|preciso|gostaria)\b.*\b(agendar|marcar)\b",
            r"\b(tem|h\u00e1)\b.*\b(hor\u00e1rio|vaga)\b.*\b(dispon\u00edvel|livre)\b",
            r"\b(disponibilidade|agenda)\b",
        ],
        "keywords": ["agendar", "marcar", "reservar", "hor\u00e1rio", "consulta", "atendimento"],
        "weight": 1.0,
    },
    "scheduling_reschedule": {
        "category": IntentCategory.SCHEDULING,
        "patterns": [
            r"\b(remarcar|reagendar|alterar)\b.*\b(consulta|hor\u00e1rio|data)\b",
            r"\b(mudar|trocar)\b.*\b(hor\u00e1rio|dia|data)\b",
        ],
        "keywords": ["remarcar", "reagendar", "alterar", "mudar", "trocar"],
        "weight": 0.9,
    },
    
    # Cancellation intents
    "cancellation_request": {
        "category": IntentCategory.CANCELLATION,
        "patterns": [
            r"\b(cancelar|desmarcar)\b.*\b(consulta|hor\u00e1rio|agendamento)\b",
            r"\b(n\u00e3o|nao)\b.*\b(vou|posso|poderei)\b.*\b(ir|comparecer)\b",
        ],
        "keywords": ["cancelar", "desmarcar", "desistir"],
        "weight": 1.0,
    },
    
    # Confirmation intents
    "confirmation_positive": {
        "category": IntentCategory.CONFIRMATION,
        "patterns": [
            r"^(sim|ok|pode|confirmo|confirmado|certo|correto|isso|exato)$",
            r"\b(sim|ok)\b.*\b(pode|confirmo)\b",
            r"\b(est\u00e1|t\u00e1)\b.*\b(bom|\u00f3timo|perfeito|certo)\b",
        ],
        "keywords": ["sim", "ok", "confirmo", "confirmado", "certo", "correto", "perfeito"],
        "weight": 0.9,
    },
    "confirmation_negative": {
        "category": IntentCategory.CONFIRMATION,
        "patterns": [
            r"^(n\u00e3o|nao|negativo|errado|incorreto)$",
            r"\b(n\u00e3o|nao)\b.*\b(\u00e9|est\u00e1)\b.*\b(isso|correto)\b",
        ],
        "keywords": ["n\u00e3o", "nao", "negativo", "errado", "incorreto"],
        "weight": 0.9,
    },
    
    # Question intents
    "question_price": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(quanto|qual)\b.*\b(custa|valor|pre\u00e7o|custo)\b",
            r"\b(pre\u00e7o|valor|custo)\b.*\b(consulta|atendimento|sess\u00e3o)\b",
            r"\b(valores|pre\u00e7os)\b",
        ],
        "keywords": ["quanto", "valor", "pre\u00e7o", "custo", "custa"],
        "weight": 1.0,
    },
    "question_location": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(onde|qual)\b.*\b(fica|endere\u00e7o|localiza\u00e7\u00e3o|local)\b",
            r"\b(endere\u00e7o|localiza\u00e7\u00e3o)\b.*\b(cl\u00ednica|consult\u00f3rio)\b",
            r"\b(como)\b.*\b(chegar|chego)\b",
        ],
        "keywords": ["onde", "endere\u00e7o", "localiza\u00e7\u00e3o", "local", "chegar"],
        "weight": 1.0,
    },
    "question_hours": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(hor\u00e1rio|hora)\b.*\b(funciona|atende|abre|fecha)\b",
            r"\b(que horas?)\b.*\b(abre|fecha|funciona)\b",
            r"\b(funcionamento|expediente)\b",
        ],
        "keywords": ["hor\u00e1rio", "funciona", "abre", "fecha", "funcionamento"],
        "weight": 1.0,
    },
    "question_services": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(quais?|que)\b.*\b(servi\u00e7os?|procedimentos?|especialidades?)\b",
            r"\b(voc\u00eas|voces)\b.*\b(fazem|oferecem|atendem)\b",
            r"\b(atende|faz)\b.*\b(tipo|especialidade)\b",
        ],
        "keywords": ["servi\u00e7os", "procedimentos", "especialidades", "fazem", "oferecem"],
        "weight": 1.0,
    },
    "question_insurance": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(aceita|aceitar)\b.*\b(conv\u00eanio|plano|seguro)\b",
            r"\b(conv\u00eanio|plano)\b.*\b(aceito|atendido)\b",
            r"\b(particular|convenio)\b",
        ],
        "keywords": ["conv\u00eanio", "plano", "seguro", "particular"],
        "weight": 1.0,
    },
    "question_general": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"^(o que|como|quando|onde|por que|qual|quais|quem)\b",
            r"\?$",
        ],
        "keywords": ["o que", "como", "quando", "onde", "qual", "quem"],
        "weight": 0.6,
    },
    
    # Greeting intents
    "greeting_hello": {
        "category": IntentCategory.GREETING,
        "patterns": [
            r"^(ol\u00e1|ola|oi|hey|e a\u00ed|eai|bom dia|boa tarde|boa noite|al\u00f4|alo)\b",
            r"^(oi|ol\u00e1|ola)\s*[!.,]?$",
        ],
        "keywords": ["ol\u00e1", "ola", "oi", "bom dia", "boa tarde", "boa noite"],
        "weight": 0.95,
    },
    
    # Farewell intents
    "farewell_bye": {
        "category": IntentCategory.FAREWELL,
        "patterns": [
            r"\b(tchau|adeus|at\u00e9|ate)\b.*\b(logo|mais|breve)?\b",
            r"^(tchau|adeus|at\u00e9 logo|at\u00e9 mais|flw|vlw)$",
            r"\b(obrigad[oa])\b.*\b(ajuda)?\b",
        ],
        "keywords": ["tchau", "adeus", "at\u00e9 logo", "obrigado", "obrigada", "valeu"],
        "weight": 0.9,
    },
    
    # Complaint intents
    "complaint_general": {
        "category": IntentCategory.COMPLAINT,
        "patterns": [
            r"\b(reclamar|reclama\u00e7\u00e3o|problema|insatisfeito|insatisfa\u00e7\u00e3o)\b",
            r"\b(p\u00e9ssimo|horr\u00edvel|terr\u00edvel|ruim|mal)\b.*\b(atendimento|servi\u00e7o)\b",
            r"\b(quero|preciso)\b.*\b(falar|conversar)\b.*\b(gerente|respons\u00e1vel|supervisor)\b",
        ],
        "keywords": ["reclamar", "reclama\u00e7\u00e3o", "problema", "insatisfeito", "p\u00e9ssimo"],
        "weight": 1.0,
    },
    "complaint_wait": {
        "category": IntentCategory.COMPLAINT,
        "patterns": [
            r"\b(demora|demorado|esperando|espera)\b",
            r"\b(muito)\b.*\b(tempo|espera)\b",
        ],
        "keywords": ["demora", "demorado", "esperando", "espera"],
        "weight": 0.8,
    },
    
    # Information intents
    "information_provide": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(meu|minha)\b.*\b(nome|cpf|telefone|email|e-mail)\b.*\b(\u00e9|e)\b",
            r"^\d{3}[\.\-]?\d{3}[\.\-]?\d{3}[\.\-]?\d{2}$",  # CPF
            r"^\(\d{2}\)\s?\d{4,5}[\-\s]?\d{4}$",  # Phone
        ],
        "keywords": ["meu nome", "meu cpf", "meu telefone"],
        "weight": 0.85,
    },
    
    # Feedback intents
    "feedback_positive": {
        "category": IntentCategory.FEEDBACK,
        "patterns": [
            r"\b(excelente|\u00f3timo|otimo|maravilhoso|perfeito|amei|adorei)\b",
            r"\b(muito)\b.*\b(bom|legal|satisfeito)\b",
            r"\b(parab\u00e9ns|parabens)\b",
        ],
        "keywords": ["excelente", "\u00f3timo", "maravilhoso", "parab\u00e9ns", "amei"],
        "weight": 0.9,
    },
    "feedback_negative": {
        "category": IntentCategory.FEEDBACK,
        "patterns": [
            r"\b(p\u00e9ssimo|horr\u00edvel|terr\u00edvel|decepcionado|frustrado)\b",
            r"\b(muito)\b.*\b(ruim|mal)\b",
        ],
        "keywords": ["p\u00e9ssimo", "horr\u00edvel", "terr\u00edvel", "decepcionado"],
        "weight": 0.9,
    },
    
    # Emergency intents
    "emergency_medical": {
        "category": IntentCategory.EMERGENCY,
        "patterns": [
            r"\b(emerg\u00eancia|emergencia|urgente|urg\u00eancia|urgencia)\b",
            r"\b(passando)\b.*\b(mal)\b",
            r"\b(dor)\b.*\b(forte|intensa|muita)\b",
            r"\b(socorro|ajuda)\b.*\b(urgente)?\b",
        ],
        "keywords": ["emerg\u00eancia", "urgente", "urg\u00eancia", "passando mal", "socorro"],
        "weight": 1.0,
    },
    
    # Handoff request
    "handoff_request": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(falar|conversar|atendimento)\b.*\b(humano|pessoa|atendente|real)\b",
            r"\b(quero|preciso)\b.*\b(falar|conversar)\b.*\b(algu\u00e9m|alguem)\b",
            r"\b(n\u00e3o|nao)\b.*\b(robo|rob\u00f4|bot|m\u00e1quina|maquina)\b",
        ],
        "keywords": ["atendente", "humano", "pessoa real", "n\u00e3o rob\u00f4"],
        "weight": 1.0,
    },
    # Official intents (doc) - pós-atendimento e risco
    "pos_atendimento_duvida": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(d\u00favida|duvida)\b.*\b(p\u00f3s|pos)\b.*\b(atendimento|consulta|cirurgia)\b",
            r"\b(ap\u00f3s|apos)\b.*\b(atendimento)\b.*\b(pode|devo)\b",
        ],
        "keywords": ["p\u00f3s atendimento", "d\u00favida depois", "ap\u00f3s consulta"],
        "weight": 1.0,
    },
    "pos_atendimento_sintomas_preocupantes": {
        "category": IntentCategory.EMERGENCY,
        "patterns": [
            r"\b(dor)\b.*\b(forte|intensa)\b.*\b(sangramento|sangrar)\b",
            r"\b(sangramento|sangrar)\b.*\b(ap\u00f3s|depois)\b",
            r"\b(febre|piora)\b.*\b(ap\u00f3s|depois)\b.*\b(cirurgia|procedimento)\b",
            r"\b(emerg\u00eancia|emergencia|urgente)\b",
        ],
        "keywords": ["dor forte", "sangramento", "febre", "piora", "emerg\u00eancia"],
        "weight": 1.0,
    },
    "pedido_alterar_medicacao": {
        "category": IntentCategory.EMERGENCY,
        "patterns": [
            r"\b(diminuir|aumentar)\b.*\b(dose|rem\u00e9dio|remedio|medicamento)\b",
            r"\b(rem\u00e9dio|remedio)\b.*\b(forte|fraco)\b.*\b(dose)\b",
            r"\b(pode)\b.*\b(tomar|suspender)\b.*\b(menos|mais)\b",
        ],
        "keywords": ["alterar dose", "diminuir rem\u00e9dio", "medicamento forte"],
        "weight": 1.0,
    },
    "reclamacao": {
        "category": IntentCategory.COMPLAINT,
        "patterns": [
            r"\b(reclamar|reclama\u00e7\u00e3o|problema|insatisfeito)\b",
            r"\b(p\u00e9ssimo|horr\u00edvel|ruim)\b.*\b(atendimento|servi\u00e7o)\b",
        ],
        "keywords": ["reclamar", "reclama\u00e7\u00e3o", "problema", "insatisfeito"],
        "weight": 1.0,
    },
    "conduta_inadequada_ameaca": {
        "category": IntentCategory.COMPLAINT,
        "patterns": [
            r"\b(processar|processo)\b.*\b(juiz|advogado)\b",
            r"\b(absurdo|vergonha)\b.*\b(vou)\b.*\b(processar)\b",
            r"\b(amea\u00e7a|ameaca)\b",
        ],
        "keywords": ["processar", "advogado", "absurdo", "amea\u00e7a"],
        "weight": 1.0,
    },
    "documentos_e_comprovantes": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(documento|comprovante|atestado|receita)\b",
            r"\b(envia)\b.*\b(documento|receita)\b",
        ],
        "keywords": ["documento", "comprovante", "atestado", "receita"],
        "weight": 0.9,
    },
    "orcamento_existente": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(or\u00e7amento|orcamento)\b.*\b(anterior|que fiz)\b",
            r"\b(consulta)\b.*\b(pre\u00e7o|valor)\b.*\b(passada)\b",
        ],
        "keywords": ["or\u00e7amento", "or\u00e7amento anterior"],
        "weight": 0.9,
    },
    "cobranca_renegociacao": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(parcelar|parcela)\b.*\b(d\u00e9bito|debito)\b",
            r"\b(renegociar|negociar)\b.*\b(cobran\u00e7a|cobranca)\b",
        ],
        "keywords": ["parcelar", "renegociar", "cobran\u00e7a"],
        "weight": 0.9,
    },
    "reagendar": {
        "category": IntentCategory.SCHEDULING,
        "patterns": [
            r"\b(remarcar|reagendar|alterar)\b.*\b(consulta|hor\u00e1rio|data)\b",
            r"\b(mudar|trocar)\b.*\b(hor\u00e1rio|dia|data)\b",
        ],
        "keywords": ["remarcar", "reagendar", "alterar", "mudar"],
        "weight": 0.9,
    },
    "cancelar": {
        "category": IntentCategory.CANCELLATION,
        "patterns": [
            r"\b(cancelar|desmarcar)\b.*\b(consulta|hor\u00e1rio|agendamento)\b",
            r"\b(n\u00e3o|nao)\b.*\b(vou|posso)\b.*\b(ir|comparecer)\b",
        ],
        "keywords": ["cancelar", "desmarcar", "desistir"],
        "weight": 1.0,
    },
    "confirmar_presenca": {
        "category": IntentCategory.CONFIRMATION,
        "patterns": [
            r"^(sim|ok|pode|confirmo|confirmado|certo)$",
            r"\b(sim|ok)\b.*\b(pode|confirmo)\b",
        ],
        "keywords": ["sim", "ok", "confirmo", "confirmado", "certo"],
        "weight": 0.9,
    },
    "disponibilidade_horarios": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(hor\u00e1rio|hora)\b.*\b(funciona|atende|abre|fecha)\b",
            r"\b(disponibilidade)\b.*\b(hor\u00e1rio)\b",
        ],
        "keywords": ["hor\u00e1rio", "funciona", "abre", "fecha", "disponibilidade"],
        "weight": 1.0,
    },
    "detalhes_servicos": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(quais?|que)\b.*\b(servi\u00e7os?|procedimentos?)\b",
            r"\b(onde|endere\u00e7o)\b.*\b(fica)\b",
        ],
        "keywords": ["servi\u00e7os", "procedimentos", "endere\u00e7o"],
        "weight": 1.0,
    },
    "pre_atendimento": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(pr\u00e9|pre)\b.*\b(atendimento|consulta)\b.*\b(preparo|jejum)\b",
        ],
        "keywords": ["pr\u00e9 atendimento", "preparo", "jejum"],
        "weight": 0.85,
    },
    "faq": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(pergunta)\b.*\b(frequente)\b",
            r"\b(d\u00favida)\b.*\b(comum)\b",
        ],
        "keywords": ["pergunta frequente", "d\u00favidas comuns"],
        "weight": 0.8,
    },
    "cultura_historia": {
        "category": IntentCategory.INFORMATION,
        "patterns": [
            r"\b(hist\u00f3ria|historia)\b.*\b(cl\u00ednica|clinica)\b",
            r"\b(quem)\b.*\b(s\u00e3o|sao)\b",
        ],
        "keywords": ["hist\u00f3ria", "cl\u00ednica"],
        "weight": 0.7,
    },
    "qualificacao_publico": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(p\u00fablico|publico)\b.*\b(alvo)\b",
            r"\b(atende)\b.*\b(quem)\b",
        ],
        "keywords": ["p\u00fablico", "atende quem"],
        "weight": 0.7,
    },
    "informacoes_profissionais": {
        "category": IntentCategory.QUESTION,
        "patterns": [
            r"\b(m\u00e9dico|medico|doutor|dr)\b.*\b(qual)\b",
            r"\b(profissionais)\b.*\b(equipe)\b",
        ],
        "keywords": ["m\u00e9dico", "doutor", "profissionais", "equipe"],
        "weight": 0.8,
    },
}


# ============================================
# Intent Method Configuration
# ============================================

# Intents that should use regex (structured patterns, critical cases)
REGEX_ONLY_INTENTS: set[str] = {
    "information_provide",  # CPF, telefone - padrões estruturados
    "emergency_medical",   # Crítico, precisa ser preciso
}

# Intents that should use Naive Bayes (general, variable patterns)
NAIVE_BAYES_INTENTS: set[str] = {
    "scheduling_request",
    "scheduling_reschedule",
    "cancellation_request",
    "confirmation_positive",
    "confirmation_negative",
    "question_price",
    "question_location",
    "question_hours",
    "question_services",
    "question_insurance",
    "question_general",
    "greeting_hello",
    "farewell_bye",
    "complaint_general",
    "complaint_wait",
    "feedback_positive",
    "feedback_negative",
    "handoff_request",
}


def generate_training_data_from_intents(
    intent_definitions: dict[str, dict]
) -> dict[str, list[str]]:
    """
    Generate training data for Naive Bayes from intent definitions.
    
    Creates example sentences by combining keywords and generating variations.
    
    Args:
        intent_definitions: Intent definitions dictionary
        
    Returns:
        Dictionary mapping intent names to lists of example messages
    """
    training_data: dict[str, list[str]] = {}
    
    for intent_name, intent_def in intent_definitions.items():
        examples: list[str] = []
        keywords = intent_def.get("keywords", [])
        patterns = intent_def.get("patterns", [])
        
        # Generate examples from keywords
        if keywords:
            # Simple keyword combinations
            for keyword in keywords:
                if len(keyword.split()) == 1:  # Single word
                    examples.append(keyword)
                    # Add with common prefixes
                    for prefix in ["quero", "preciso", "gostaria", "tem", "qual"]:
                        examples.append(f"{prefix} {keyword}")
                else:  # Multi-word keyword
                    examples.append(keyword)
            
            # Generate combinations of 2-3 keywords
            if len(keywords) >= 2:
                for i, kw1 in enumerate(keywords[:3]):  # Limit to avoid too many
                    for kw2 in keywords[i+1:min(i+3, len(keywords))]:
                        examples.append(f"{kw1} {kw2}")
        
        # Extract words from regex patterns
        for pattern in patterns:
            # Extract word groups from regex
            word_matches = re.findall(r'\b([a-záàâãéêíóôõúç]+)\b', pattern, re.IGNORECASE)
            if word_matches:
                # Create simple sentence from extracted words
                sentence = " ".join(word_matches[:5])  # Limit length
                if sentence and sentence not in examples:
                    examples.append(sentence)
        
        # Add common variations
        if intent_name == "scheduling_request":
            examples.extend([
                "quero agendar uma consulta",
                "preciso marcar um horário",
                "tem vaga disponível",
                "gostaria de reservar",
                "quero um slot",
                "preciso de um horário",
                "tem agenda livre",
                "quero marcar consulta",
            ])
        elif intent_name == "cancellation_request":
            examples.extend([
                "quero cancelar",
                "não vou poder ir",
                "preciso desmarcar",
                "quero cancelar a consulta",
                "não posso comparecer",
            ])
        elif intent_name == "question_price":
            examples.extend([
                "quanto custa",
                "qual o valor",
                "quanto é",
                "qual o preço",
                "quanto custa a consulta",
            ])
        elif intent_name == "greeting_hello":
            examples.extend([
                "olá",
                "oi",
                "bom dia",
                "boa tarde",
                "boa noite",
                "olá, tudo bem",
            ])
        elif intent_name == "farewell_bye":
            examples.extend([
                "tchau",
                "até logo",
                "obrigado",
                "obrigada",
                "valeu",
                "até mais",
            ])
        
        # Remove duplicates and empty strings
        training_data[intent_name] = list(set([ex.strip() for ex in examples if ex.strip()]))
    
    return training_data


class IntentClassifier:
    """
    Hybrid intent classifier using Naive Bayes + Regex patterns.
    
    Features:
    - Naive Bayes for general intents (better generalization)
    - Regex patterns for structured/critical intents (precise matching)
    - Automatic fallback between methods
    - Confidence calculation
    - Intent category mapping
    """

    def __init__(
        self,
        custom_intents: Optional[dict] = None,
        use_naive_bayes: bool = True,
        naive_bayes_threshold: float = 0.6,
    ):
        """
        Initialize the hybrid classifier.
        
        Args:
            custom_intents: Optional custom intent definitions to merge
            use_naive_bayes: Whether to use Naive Bayes for supported intents
            naive_bayes_threshold: Minimum confidence for Naive Bayes result
        """
        self.intents = INTENT_DEFINITIONS.copy()
        if custom_intents:
            self.intents.update(custom_intents)
        
        self.use_naive_bayes = use_naive_bayes
        self.naive_bayes_threshold = naive_bayes_threshold
        
        # Compile regex patterns for performance
        self._compiled_patterns: dict[str, list[re.Pattern]] = {}
        for intent_name, intent_def in self.intents.items():
            self._compiled_patterns[intent_name] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in intent_def.get("patterns", [])
            ]
        
        # Initialize Naive Bayes classifier if enabled
        self._naive_bayes: Optional[IntentNaiveBayes] = None
        if self.use_naive_bayes:
            training_data = generate_training_data_from_intents(self.intents)
            self._naive_bayes = IntentNaiveBayes(training_data=training_data)
            
            # Set intent categories
            categories = {
                intent_name: intent_def["category"]
                for intent_name, intent_def in self.intents.items()
            }
            self._naive_bayes.set_intent_categories(categories)

    def classify(self, message: str) -> IntentResult:
        """
        Classify the intent of a message using hybrid approach.
        
        Strategy:
        1. Try Naive Bayes for general intents (if enabled)
        2. Fallback to regex for all intents or if Naive Bayes confidence is low
        3. Choose best result based on confidence
        
        Args:
            message: User message to classify
            
        Returns:
            IntentResult with intent, category, and confidence
        """
        message_lower = message.strip().lower()
        
        if not message_lower:
            return IntentResult(
                intent="duvida_generica_sem_contexto",
                category=IntentCategory.UNKNOWN,
                confidence=0.0,
                matched_patterns=[],
            )
        
        # Try Naive Bayes first for supported intents
        nb_result: Optional[IntentResult] = None
        if self.use_naive_bayes and self._naive_bayes:
            nb_result = self._naive_bayes.classify(message_lower)
            
            # Use Naive Bayes if confidence is high enough
            if nb_result.confidence >= self.naive_bayes_threshold:
                # Check if this intent should use regex instead
                if nb_result.intent not in REGEX_ONLY_INTENTS:
                    intent = LEGACY_TO_OFFICIAL_INTENT.get(nb_result.intent, nb_result.intent)
                    return IntentResult(
                        intent=intent,
                        category=nb_result.category,
                        confidence=nb_result.confidence,
                        matched_patterns=nb_result.matched_patterns,
                    )
        
        # Use regex classification (for all intents or as fallback)
        regex_result = self._classify_with_regex(message_lower)
        
        # Choose best result
        if nb_result and nb_result.confidence > regex_result.confidence:
            # Use Naive Bayes if it's better, but only for non-regex-only intents
            if nb_result.intent not in REGEX_ONLY_INTENTS:
                intent = LEGACY_TO_OFFICIAL_INTENT.get(nb_result.intent, nb_result.intent)
                return IntentResult(
                    intent=intent,
                    category=nb_result.category,
                    confidence=nb_result.confidence,
                    matched_patterns=nb_result.matched_patterns,
                )
        
        intent = LEGACY_TO_OFFICIAL_INTENT.get(regex_result.intent, regex_result.intent)
        return IntentResult(
            intent=intent,
            category=regex_result.category,
            confidence=regex_result.confidence,
            matched_patterns=regex_result.matched_patterns,
        )
    
    def _classify_with_regex(self, message: str) -> IntentResult:
        """Classify using regex patterns (original method)."""
        best_intent = "unknown"
        best_category = IntentCategory.UNKNOWN
        best_score = 0.0
        matched_patterns: list[str] = []
        
        for intent_name, intent_def in self.intents.items():
            score, patterns = self._score_intent(message, intent_name, intent_def)
            
            if score > best_score:
                best_score = score
                best_intent = intent_name
                best_category = intent_def["category"]
                matched_patterns = patterns
        
        # Calculate confidence (normalize score to 0-1)
        confidence = min(best_score, 1.0)
        # Map to official intent (23 canônicos)
        official_intent = LEGACY_TO_OFFICIAL_INTENT.get(best_intent, best_intent)
        
        return IntentResult(
            intent=official_intent,
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
        """
        Calculate score for an intent based on pattern and keyword matching.
        
        Returns:
            Tuple of (score, matched_patterns)
        """
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
        use_naive_bayes: bool = True,
    ) -> None:
        """
        Add a custom intent definition.
        
        Args:
            name: Intent name
            category: Intent category
            patterns: Regex patterns
            keywords: Keywords for matching
            weight: Pattern weight
            use_naive_bayes: Whether to use Naive Bayes for this intent
        """
        self.intents[name] = {
            "category": category,
            "patterns": patterns,
            "keywords": keywords,
            "weight": weight,
        }
        self._compiled_patterns[name] = [
            re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns
        ]
        
        # Update Naive Bayes if enabled
        if self.use_naive_bayes and self._naive_bayes and use_naive_bayes:
            # Generate examples for this intent
            training_data = generate_training_data_from_intents({name: self.intents[name]})
            if name in training_data and training_data[name]:
                self._naive_bayes.update_training_data(name, training_data[name])
                # Update category mapping
                categories = self._naive_bayes._intent_categories.copy()
                categories[name] = category
                self._naive_bayes.set_intent_categories(categories)


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

