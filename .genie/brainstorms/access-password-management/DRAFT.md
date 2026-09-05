# DRAFT — Gerenciamento de senha de acesso

## Pedido original

> O builder e a clínica precisam ter liberdade para alterar as senhas de acesso dos
> próprios perfis. O master continua podendo alterar o e-mail de login e a senha de
> acesso de todos.

## WRS

```
WRS: ██████████ 100/100
 Problem ✅ | Scope ✅ | Decisions ✅ | Risks ✅ | Criteria ✅
```

## Problema

Builders e administradores de clínica dependem hoje do master para trocar a própria
senha de acesso, embora sejam os titulares autenticados dessas contas. Eles precisam
de um fluxo seguro de autoatendimento para troca de senha, sem perder ou ampliar a
autoridade global já existente do master sobre e-mail de login e senha.

## Estado atual levantado no código

- O modelo `User` possui um único `password_hash` bcrypt e os papéis
  `clinic_admin`, `builder`, `super_admin` e `investor`.
- `POST /v1/auth/login` é o único endpoint de autenticação; não há endpoint autenticado
  para troca da própria senha.
- O master já consegue alterar e-mail e senha de clínicas por
  `PATCH /v1/admin/clinics/{clinic_id}` e de builders por
  `PATCH /v1/admin/builders/{builder_id}`.
- A tela `frontend/artifacts/neurostack/src/pages/master-admin.tsx` já expõe os campos
  de e-mail e nova senha ao master.
- O painel mostra o e-mail da conta no rodapé, mas não oferece área de perfil/conta
  para builder ou clínica.
- No estado atual há, em regra, um usuário principal `builder` por builder e um
  `clinic_admin` por tenant de clínica; as rotas administrativas selecionam apenas o
  primeiro usuário correspondente ao papel.

## Escopo em refinamento

### IN já confirmado

- Builder autenticado altera a senha do próprio usuário de acesso.
- Administrador de clínica autenticado altera a senha do próprio usuário de acesso.
- Builder autenticado redefine a senha do usuário administrador principal de qualquer
  clínica atualmente vinculada ao seu `builder_id`.
- Master mantém a capacidade atual de alterar e-mail de login e senha de builders e
  clínicas.
- Isolamento por identidade autenticada: nenhum builder ou clínica pode alterar a
  credencial de outra conta.
- O autoatendimento e a redefinição delegada não exigem a senha atual.

### OUT confirmado

- Alteração de e-mail por builder ou clínica; o pedido reserva essa capacidade ao master.
- Recuperação de senha por e-mail (“esqueci minha senha”).
- Criação de novos usuários, múltiplos administradores ou alteração de papéis.
- Redefinição de senha de clínicas não vinculadas ao builder autenticado, mesmo que o
  cliente tente enviar manualmente o identificador delas.
- Autoatendimento de senha para `investor`; o papel é somente leitura e não foi citado
  no pedido.
- Autoatendimento de senha para `super_admin`; nesta entrega, a gestão do master sobre
  “todos” significa builders e clínicas.

## Abordagens candidatas

### A — Autoatendimento mais redefinição delegada por clínica vinculada (recomendada)

- Criar um contrato autenticado, por exemplo `PATCH /v1/auth/me/password`, que sempre
  resolve o usuário pelo `user_id` do JWT e nunca recebe `user_id`, `tenant_id` ou
  `builder_id` do cliente.
- Receber nova senha e confirmação da nova senha, sem exigir a senha atual.
- Adicionar uma área compacta “Segurança da conta” acessível a builder e clínica.
- Criar um segundo contrato em
  `PATCH /v1/builder/clinics/{clinic_id}/password`; ele só redefine o usuário
  `clinic_admin` principal quando o tenant-alvo tem o mesmo `builder_id` autenticado.
- Expor “Alterar senha” em cada clínica vinculada na tela do builder, sem campo de e-mail.
- Manter as rotas do master como estão, com edição direta de e-mail e senha.

Simplicidade: dois endpoints com alvos distintos, dois pontos de entrada de interface e
nenhum estado persistente novo, token de recuperação, fila, e-mail ou configuração.

### B — Rotas de autoatendimento separadas para builder e clínica

- Criar rotas específicas em `/v1/builder/...` e `/v1/clinic/...`.
- Facilita textos e políticas divergentes no futuro, mas duplica contrato, validação,
  autorização e testes sem uma necessidade atual que pague esse custo.

### C — Reutilizar as rotas administrativas do master

- Relaxar a autorização de `PATCH /v1/admin/clinics/{clinic_id}` para builders quando a
  clínica estiver vinculada.
- Evita uma URL nova, porém mistura o contrato amplo do master — que aceita nome, e-mail
  e senha — com a permissão estreita do builder. Exige bloqueios campo a campo e aumenta
  o risco de conceder alteração de e-mail ou nome por engano.

## Simplicity Gate

O desenho completo mais simples é a abordagem A: uma rota de senha para o próprio
usuário e uma rota de senha para a clínica vinculada, mantendo a autorização estreita e
preservando integralmente as rotas administrativas do master. O segundo endpoint é pago
pelo requisito confirmado de administração delegada; nenhuma rota genérica de usuário é
necessária. Não há requisito presente para recuperação por e-mail, histórico de senhas,
MFA ou revogação distribuída. Esses mecanismos ficam fora até existir uma demanda
mensurável.

## Riscos e restrições

1. **Escalada horizontal:** aceitar IDs enviados pelo cliente pode permitir troca de
   senha de outro tenant ou builder; a identidade-alvo deve vir exclusivamente do JWT.
2. **Sessões existentes:** JWTs são stateless; trocar o hash não invalida tokens já
   emitidos sem um novo estado persistente. O navegador que troca a própria senha pode
   encerrar sua sessão, mas tokens em outros dispositivos continuam válidos por até 24h.
3. **Tomada de conta em sessão aberta:** como a decisão é não exigir a senha atual,
   qualquer sessão deixada aberta pode definir uma senha nova; o encerramento local após
   a troca reduz exposição naquele navegador, mas não revoga outros JWTs.
4. **Política inconsistente:** os contratos administrativos atuais exigem apenas seis
   caracteres. O autoatendimento não deve introduzir uma política divergente por acaso.
5. **Estado do cliente:** se algum dia o e-mail puder ser alterado fora do master, o
   contexto/localStorage precisará ser sincronizado; isso não se aplica ao escopo atual
   de senha apenas.

## Critérios de aceitação iniciais

1. Dado um `builder` autenticado, ao informar e confirmar uma nova senha válida, o login
   com a nova senha funciona e o login com a anterior falha.
2. O mesmo comportamento vale para um `clinic_admin` autenticado.
3. Confirmação divergente ou nova senha fora da política não altera o hash persistido e
   produz erro seguro, sem expor hashes ou credenciais.
4. O endpoint de autoatendimento não aceita nem usa identificadores de outro usuário,
   tenant, builder ou clínica.
5. `super_admin` continua conseguindo alterar o e-mail e a senha de builders e clínicas
   pelas rotas e controles administrativos existentes.
6. Um builder consegue redefinir a senha de uma clínica vinculada e não consegue
   redefinir a senha de uma clínica não vinculada, desvinculada ou ligada a outro builder.
7. A redefinição feita pelo builder não permite alterar e-mail, nome, papel ou qualquer
   outro campo do usuário ou da clínica.
8. Ao trocar a própria senha pela interface, builder ou clínica é desconectado do
   navegador atual e precisa entrar novamente com a nova senha.
9. Ao redefinir a senha de uma clínica, o builder permanece autenticado em sua própria
   sessão.

## Perguntas abertas

Nenhuma. Decisões confirmadas:

- JWTs já emitidos permanecem válidos até a expiração atual de, no máximo, 24 horas.
- “Alterar minha senha” abre em modal pelo e-mail no rodapé para builder e clínica.
- Cada clínica vinculada exibe ao builder uma ação específica “Alterar senha”.
- `super_admin` não recebe autoatendimento nesta entrega; sua gestão de “todos” abrange
  builders e clínicas.
