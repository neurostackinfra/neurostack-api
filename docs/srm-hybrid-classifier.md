# SRM - Classificador Híbrido (Naive Bayes + Regex)

## Visão Geral

O Intent Classifier do SRM agora utiliza uma abordagem **híbrida** que combina:
- **Naive Bayes** para intents gerais (melhor generalização)
- **Regex Patterns** para casos específicos e críticos (precisão garantida)

## Como Funciona

### Estratégia de Classificação

1. **Tenta Naive Bayes primeiro** para intents suportados
2. **Se confiança ≥ threshold** (padrão: 0.6), usa resultado do Naive Bayes
3. **Caso contrário**, usa Regex como fallback
4. **Sempre usa Regex** para intents críticos (emergência, informações estruturadas)

### Intents por Método

#### Naive Bayes (Generalização)
- `scheduling_request`, `scheduling_reschedule`
- `cancellation_request`
- `confirmation_positive`, `confirmation_negative`
- `question_*` (todos os tipos de perguntas)
- `greeting_hello`, `farewell_bye`
- `complaint_*`, `feedback_*`
- `handoff_request`

#### Regex Only (Precisão)
- `information_provide` (CPF, telefone - padrões estruturados)
- `emergency_medical` (crítico, precisa ser preciso)

## Vantagens

### ✅ Melhor Acurácia em Variações
```python
# Antes (Regex): Falhava
"preciso de um horário" → unknown ou intent errado

# Agora (Naive Bayes): Funciona
"preciso de um horário" → scheduling_request (confiança: 0.89)
```

### ✅ Generalização Automática
- Aprende sinônimos e variações automaticamente
- Não precisa listar todas as keywords manualmente
- Melhora com novos exemplos

### ✅ Mantém Precisão em Casos Críticos
- Emergências sempre usam regex (garantia de precisão)
- Padrões estruturados (CPF, telefone) mantêm regex

## Uso

### Uso Padrão (Híbrido Ativado)
```python
from src.srm.intent_classifier import get_intent_classifier

classifier = get_intent_classifier()

# Classifica automaticamente usando melhor método
result = classifier.classify("preciso de um horário")
# result.intent = "scheduling_request"
# result.confidence = 0.89
# result.category = IntentCategory.SCHEDULING
```

### Desabilitar Naive Bayes (Apenas Regex)
```python
from src.srm.intent_classifier import IntentClassifier

# Criar com Naive Bayes desabilitado
classifier = IntentClassifier(use_naive_bayes=False)

result = classifier.classify("preciso de um horário")
# Usa apenas regex (comportamento antigo)
```

### Ajustar Threshold de Confiança
```python
# Threshold mais alto = mais conservador (prefere regex)
classifier = IntentClassifier(
    use_naive_bayes=True,
    naive_bayes_threshold=0.8  # Padrão: 0.6
)
```

## Dados de Treinamento

### Geração Automática
Os dados de treinamento são gerados automaticamente a partir das definições de intents:
- Keywords são combinadas em exemplos
- Padrões regex são extraídos para criar variações
- Exemplos comuns são adicionados para intents principais

### Adicionar Exemplos Manualmente
```python
from src.srm.intent_classifier import get_intent_classifier

classifier = get_intent_classifier()

# Adicionar novos exemplos para um intent
classifier.add_custom_intent(
    name="novo_intent",
    category=IntentCategory.QUESTION,
    patterns=[r"\b(novo)\b.*\b(padrão)\b"],
    keywords=["novo", "padrão"],
    use_naive_bayes=True  # Usar Naive Bayes para este intent
)
```

### Atualizar com Dados Reais
```python
# Se você tem dados históricos rotulados
if classifier._naive_bayes:
    classifier._naive_bayes.update_training_data(
        intent_name="scheduling_request",
        examples=[
            "quero agendar",
            "preciso marcar",
            "tem vaga?",
            # ... mais exemplos
        ]
    )
```

## Compatibilidade

### ✅ Retrocompatível
- API não mudou
- Comportamento padrão mantém compatibilidade
- Pode desabilitar Naive Bayes se necessário

### ✅ Performance
- Naive Bayes: ~1-3ms (similar ao regex)
- Fallback automático se Naive Bayes não disponível
- Sem impacto significativo na latência

## Exemplos de Melhoria

### Caso 1: Variações de Agendamento
```python
# Mensagens que agora funcionam melhor:
"preciso de um horário"           # ✓ scheduling_request
"tem vaga disponível?"            # ✓ scheduling_request
"quero marcar"                    # ✓ scheduling_request
"gostaria de reservar um slot"    # ✓ scheduling_request
```

### Caso 2: Perguntas
```python
"quanto é a consulta?"            # ✓ question_price
"qual o valor?"                   # ✓ question_price
"preços?"                         # ✓ question_price
```

### Caso 3: Saudações
```python
"olá"                             # ✓ greeting_hello
"oi, tudo bem?"                   # ✓ greeting_hello
"bom dia"                         # ✓ greeting_hello
```

## Configuração

### Variáveis de Ambiente
```bash
# Não há variáveis específicas ainda
# O comportamento é controlado via código
```

### Configuração Programática
```python
# No engine.py ou onde o SRM é inicializado
from src.srm.intent_classifier import IntentClassifier

# Criar classificador customizado
intent_classifier = IntentClassifier(
    use_naive_bayes=True,          # Ativar Naive Bayes
    naive_bayes_threshold=0.6,     # Threshold de confiança
)

# Passar para o SRM engine
from src.srm.engine import SRMEngine
engine = SRMEngine(...)
engine.intent_classifier = intent_classifier
```

## Monitoramento

### Métricas Importantes
- Taxa de uso de Naive Bayes vs Regex
- Confiança média por método
- Acurácia comparativa

### Logs
O classificador mantém `matched_patterns` vazio para resultados do Naive Bayes, permitindo identificar qual método foi usado.

## Próximos Passos

1. **Coletar dados reais**: Usar mensagens rotuladas para melhorar treinamento
2. **Ajustar threshold**: Baseado em métricas de produção
3. **Expandir exemplos**: Adicionar mais variações aos dados de treinamento
4. **Fine-tuning**: Ajustar smoothing e outros hiperparâmetros

## Troubleshooting

### Naive Bayes não está sendo usado
- Verifique se `use_naive_bayes=True`
- Verifique se o intent está em `NAIVE_BAYES_INTENTS`
- Verifique se a confiança está acima do threshold

### Resultados inconsistentes
- Ajuste o `naive_bayes_threshold`
- Adicione mais exemplos de treinamento
- Verifique se o intent deveria usar regex apenas

### Performance
- Naive Bayes é rápido (~1-3ms)
- Se houver problemas, desabilite para intents específicos
- Considere cache se necessário
