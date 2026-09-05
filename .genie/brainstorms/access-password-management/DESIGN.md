# Design: Gerenciamento de senha de acesso

| Field | Value |
|-------|-------|
| **Slug** | `access-password-management` |
| **Date** | 2026-09-05 |
| **WRS** | 100/100 |

## Problem

Builders e administradores de clínica dependem do master para alterar a própria senha,
e builders não conseguem redefinir a senha das clínicas que administram. Isso reduz a
autonomia operacional e transforma uma rotina de credenciais em suporte centralizado,
sem necessidade de retirar do master sua autoridade atual.

## Scope

### IN

- Permitir que o usuário autenticado com papel `builder` ou `clinic_admin` defina uma
  nova senha para o próprio usuário, sem informar a senha atual.
- Desconectar o navegador atual depois da troca da própria senha e exigir novo login.
- Permitir que um builder autenticado redefina a senha do usuário `clinic_admin`
  principal de uma clínica atualmente vinculada ao seu `builder_id`.
- Manter o builder autenticado quando ele redefinir a senha de uma clínica vinculada.
- Disponibilizar “Alterar minha senha” em modal acionado pelo e-mail no rodapé para
  builder e clínica, além de “Alterar senha” em cada clínica vinculada na tela do builder.
- Preservar a capacidade atual do master de alterar e-mail e senha de builders e clínicas.
- Revalidar no banco a identidade, atividade, papel, tenant e vínculo atuais do usuário
  antes de qualquer mutação de senha.
- Publicar contratos OpenAPI Bearer-only com respostas estáveis e validação que não ecoa
  as senhas recebidas.
- Localizar os novos controles em português e inglês e preservar acessibilidade por
  teclado e leitor de tela.
- Cobrir regras de autorização, validação, persistência e interface com testes.

### OUT

- Permitir que builder ou clínica altere e-mail, nome, papel ou outro dado de perfil.
- Permitir que builder redefina senha de clínica não vinculada, desvinculada ou
  vinculada a outro builder.
- Autoatendimento de senha para `super_admin` ou `investor`.
- Recuperação de senha por e-mail, MFA, histórico de senhas ou múltiplos administradores
  por clínica.
- Revogação imediata de JWTs já emitidos em outros navegadores ou dispositivos.
- Mudança da política atual de senha, que exige no mínimo seis caracteres.
- Atualização do documento legado de contratos API-key; o OpenAPI é a referência para
  estas rotas autenticadas do painel.

## Approach

Adicionar dois contratos estreitos e separados. `PATCH /v1/auth/me/password` resolve o
alvo exclusivamente pelo `user_id` autenticado e aceita apenas usuários `builder` e
`clinic_admin`. Antes de escrever, a rota recarrega esse `User` no banco e confirma que
ele continua ativo, no mesmo tenant e com um papel permitido. A rota
`PATCH /v1/builder/clinics/{clinic_id}/password` também recarrega o builder pelo `sub`
do JWT, confirma papel, atividade, tenant e `builder_id` atuais e não usa o
`builder_id` antigo contido no token como autoridade final. Só então atualiza a clínica
cujo vínculo atual coincide com o builder e que possui exatamente um usuário com papel
`clinic_admin`.

Ambos os contratos recebem somente `new_password` e `confirm_password`, rejeitam campos
extras, aplicam a política existente de seis caracteres, armazenam somente o hash bcrypt
e retornam `200 {"ok": true}`. Um tratamento de validação específico para essas rotas
remove o valor `input` dos erros do Pydantic e devolve somente códigos e nomes de campos;
nenhuma senha pode aparecer em resposta ou log.

| Situação | Resposta contratual |
|---|---|
| Sucesso | `200 {"ok": true}` |
| JWT ausente, inválido ou expirado | `401`, envelope de autenticação já existente |
| API key, papel proibido, usuário ausente/inativo ou claims divergentes do banco | `403 {"detail": {"error": "password_change_forbidden"}}` |
| Clínica inexistente, não vinculada, desvinculada ou de outro builder | `404 {"detail": {"error": "clinic_not_available"}}`, indistinguível entre os casos |
| Clínica vinculada sem exatamente um `clinic_admin` | `409 {"detail": {"error": "clinic_admin_unavailable"}}`, sem mutação |
| Senha curta, confirmação divergente, tipo inválido ou campo extra | `422 {"detail": {"error": "invalid_password_payload", "fields": [...]}}`, sem incluir valores recebidos |

No OpenAPI, as duas operações anunciam apenas Bearer JWT e não exibem `X-API-Key` como
obrigatório ou suficiente. O OpenAPI será a referência dessas rotas de painel; o
documento legado de contratos API-key permanece fora desta entrega.

Na interface compartilhada, o e-mail no rodapé abre o modal “Alterar minha senha” apenas
para builder e clínica. Após sucesso, o cliente remove o JWT local e direciona ao login.
Na tela do builder, cada clínica vinculada ganha uma ação “Alterar senha”. O modal exibe
o nome da clínica selecionada, envia exatamente o ID desse card, bloqueia submissões
duplicadas e, em erro, permanece aberto sem desconectar o builder; somente o sucesso
fecha e limpa os campos. O master continua usando as rotas e controles administrativos
existentes, sem mudança de autorização.

A alternativa de relaxar `PATCH /v1/admin/clinics/{clinic_id}` foi rejeitada porque esse
contrato também aceita nome e e-mail; autorização condicional por campo ampliaria o risco
de privilégio acidental. Rotas separadas por papel para a própria senha foram rejeitadas
porque duplicariam a mesma operação baseada na identidade do JWT. Uma API genérica de
gerenciamento de usuários também foi rejeitada por ampliar o escopo além das duas ações
necessárias.

## Simplicity Case

- **Simplest complete design:** um endpoint de senha do usuário autenticado, um endpoint
  builder-scoped para a clínica vinculada e dois pontos de entrada de interface que
  reutilizam o mesmo formulário de nova senha e confirmação.
- **Added machinery:** o segundo endpoint existe apenas porque o requisito atual concede
  ao builder administração delegada sobre as clínicas vinculadas; recarregar o usuário e
  o vínculo no banco evita que claims antigos concedam poder de escrita; o envelope de
  validação sem `input` é exigido para que senhas rejeitadas não vazem em respostas.
- **Deferred until measured:** revogação global de sessões só será reconsiderada se o
  produto exigir encerramento imediato em todos os dispositivos; múltiplos usuários por
  clínica só serão tratados quando o modelo de conta passar a expor esse conceito.
- **Complexity removed:** nenhuma tabela, migração, token de recuperação, envio de e-mail,
  blacklist de JWT, `token_version`, configuração por papel, fila ou estado de retry.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Builder e clínica podem trocar a própria senha sem informar a senha atual. | Decisão explícita do produto; autenticação JWT válida é a prova de autoridade aceita para esta entrega. |
| 2 | Builder pode redefinir a senha da clínica vinculada, mas não o e-mail nem outros campos. | Concede a autonomia pedida com privilégio mínimo e mantém e-mail sob controle exclusivo do master. |
| 3 | O alvo do autoatendimento vem somente do `user_id` autenticado. | Impede que identificadores controlados pelo cliente produzam troca horizontal de senha. |
| 4 | O alvo delegado exige vínculo atual entre `Tenant.builder_id` e o builder autenticado. | Impede acesso a clínicas de outro builder ou que já tenham sido desvinculadas. |
| 5 | Contratos de senha são separados das rotas administrativas amplas do master. | Evita autorização campo a campo em endpoints que também editam nome e e-mail. |
| 6 | Nova senha e confirmação são validadas; a política continua em mínimo de seis caracteres. | Evita erro de digitação sem criar uma política divergente das rotas existentes. |
| 7 | Troca da própria senha encerra apenas a sessão do navegador atual; redefinição de clínica não encerra a sessão do builder. | Atende a experiência aprovada sem introduzir estado de revogação de JWT. |
| 8 | JWTs emitidos anteriormente permanecem válidos até expirar, em no máximo 24 horas. | Limitação explicitamente aceita do mecanismo stateless atual. |
| 9 | Autoatendimento abrange apenas `builder` e `clinic_admin`. | “Todos”, para a autoridade do master nesta entrega, foi definido como builders e clínicas; investor e master próprio ficam fora. |
| 10 | As rotas recarregam o usuário autenticado e derivam do banco atividade, papel, tenant e `builder_id` atuais antes da escrita. | Um JWT válido, porém emitido antes de desligamento, exclusão ou mudança de vínculo, não pode preservar autoridade obsoleta de reset. |
| 11 | O schema de senha rejeita campos extras e seus erros nunca incluem valores recebidos. | Impede mass assignment e evita que a validação padrão ecoe senhas em texto puro. |
| 12 | Cada clínica deve ter exatamente um `clinic_admin` para reset delegado. | O modelo não possui marcador de usuário principal; falhar de forma controlada é mais seguro que escolher uma conta arbitrária. |
| 13 | As operações são Bearer-only no OpenAPI e usam uma matriz estável de respostas. | Evita anunciar API key como suficiente e oferece contrato determinístico ao frontend e aos testes. |

## Risks & Assumptions

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | Builder tentar redefinir senha de clínica fora do seu vínculo. | High | Resolver o builder pelo JWT e combinar `Tenant.id == clinic_id` com `Tenant.builder_id == authenticated_builder_id` na mesma consulta; testar vínculo alheio e removido. |
| 2 | Uma sessão aberta ser usada para trocar senha sem conhecimento da senha atual. | High | Risco aceito pelo produto; limitar a operação aos papéis previstos, recarregar o usuário ativo do banco e desconectar imediatamente o navegador que troca a própria senha. |
| 3 | JWTs antigos continuarem autorizados após a troca ou redefinição. | Medium | Comportamento aceito e documentado: enquanto o usuário continuar ativo e com o mesmo papel/vínculo, inclusive após um reset pelo master ou builder, um JWT anterior ainda pode trocar a senha por até 24 horas. |
| 4 | Contrato delegado permitir alteração de e-mail ou perfil por mass assignment. | High | Schema específico com `extra="forbid"`, contendo somente nova senha e confirmação; não reutilizar `UpdateClinicRequest`. |
| 5 | Clínica vinculada não possuir exatamente um `clinic_admin` principal. | Medium | Preservar a premissa atual de uma conta principal e retornar erro controlado se ela não existir ou for ambígua; suporte a múltiplos administradores permanece OUT. |
| 6 | Política de senha divergir entre master, autoatendimento e builder. | Medium | Reutilizar a regra atual de mínimo seis caracteres e cobrir os três fluxos em testes de contrato. |
| 7 | Hash ou nova senha aparecer em resposta, erro de validação, log ou estado persistido do frontend. | High | Retorno opaco, handler que remove `input` dos erros, ausência de logging do body, campos controlados limpos após uso e testes com valores-sentinela. |
| 8 | Claims antigos manterem autoridade após usuário ser desativado ou builder mudar de vínculo. | High | Recarregar o usuário pelo `sub` e derivar papel, atividade, tenant e `builder_id` atuais do banco na transação da escrita. |
| 9 | OpenAPI anunciar API key como suficiente para uma mutação de credencial. | Medium | Sobrescrever a segurança dessas operações e isentá-las da injeção global de `X-API-Key`; testar o schema gerado. |

## Success Criteria

- [ ] Builder autenticado troca a própria senha válida; a senha anterior deixa de
  autenticar, a nova autentica e o navegador que fez a troca volta ao login.
- [ ] Clínica autenticada troca a própria senha com o mesmo comportamento.
- [ ] `super_admin`, `investor`, token por API key e requisição sem JWT não conseguem
  usar o endpoint de autoatendimento.
- [ ] Confirmação divergente e senha com menos de seis caracteres não alteram o hash e
  retornam erro de validação sem expor credenciais.
- [ ] Tipo inválido e campos extras são rejeitados; valores-sentinela de senha nunca
  aparecem em respostas de sucesso/erro nem em logs capturados.
- [ ] JWT de usuário removido, inativo, com papel alterado, tenant divergente ou
  `builder_id` alterado desde a emissão não consegue realizar a escrita.
- [ ] Builder redefine a senha do `clinic_admin` principal de uma clínica atualmente
  vinculada e permanece autenticado após o sucesso.
- [ ] Builder recebe negação sem alteração persistida ao apontar para clínica de outro
  builder, clínica sem vínculo, clínica desvinculada ou identificador inexistente.
- [ ] O contrato delegado não aceita nem altera e-mail, nome, papel ou outro campo.
- [ ] Clínica vinculada com zero ou mais de um `clinic_admin` retorna o erro controlado
  definido, sem alterar nenhum usuário.
- [ ] O modal de autoatendimento aparece pelo e-mail no rodapé somente para builder e
  clínica, possui textos PT/EN, acionador e diálogo operáveis por teclado, título e
  descrição acessíveis, labels associadas, `autocomplete="new-password"` e anúncio de
  erro associado aos campos.
- [ ] A tela do builder apresenta “Alterar senha” por clínica vinculada, sem campo para
  alterar e-mail; o modal identifica a clínica, envia o ID correto, impede submissão
  duplicada e permanece aberto sem logout do builder quando houver erro.
- [ ] O master continua alterando e-mail e senha de builders e clínicas pelos fluxos
  existentes, coberto por teste de não regressão.
- [ ] Nenhum endpoint ou resposta devolve senha em texto puro ou `password_hash`; JWTs
  anteriormente emitidos continuam válidos até a expiração máxima já existente de 24h.
- [ ] O OpenAPI das duas operações documenta somente Bearer JWT, sem `X-API-Key`
  obrigatório ou suficiente, e expõe a matriz estável de respostas do design.

## Next Step

Após uma revisão independente retornar SHIP, persistir e verificar a evidência vinculada
ao conteúdo deste design antes de executar `wish`.

<!-- genie-design-review:start -->
## Design Review Evidence

- **Verdict:** SHIP
- **Reviewed content SHA-256:** `5b67355832077b06ed44506b413b04c521542b38427d3d64e4b2ae57ebf88590`
- **Reviewer:** /root/password_design_review
- **Reviewed at:** 2026-09-05T16:23:49.000Z
<!-- genie-design-review:end -->
