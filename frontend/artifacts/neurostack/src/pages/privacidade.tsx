import { useEffect, useState } from "react";
import { useParams } from "wouter";
import { Loader2, Shield, AlertTriangle } from "lucide-react";

const API_BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

interface ClinicPrivData {
  clinic_id: string;
  clinic_name: string;
  clinic_legal_name: string | null;
  clinic_cnpj: string | null;
  clinic_contact: string | null;
  dpo_name: string | null;
  dpo_contact: string | null;
  lgpd_enabled: string | null;
  lgpd_custom_link: string | null;
  slm_training_consent?: boolean;
}

function today(): string {
  return new Date().toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric" });
}

export default function Privacidade() {
  const params = useParams<{ clinicId: string }>();
  const clinicId = params.clinicId;

  const [data, setData] = useState<ClinicPrivData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!clinicId) return;
    setLoading(true);
    fetch(`${API_BASE}/api/public/privacidade/${clinicId}`)
      .then((r) => {
        if (!r.ok) throw new Error("Clínica não encontrada");
        return r.json() as Promise<ClinicPrivData>;
      })
      .then((d) => { setData(d); setLoading(false); })
      .catch((e: Error) => { setError(e.message); setLoading(false); });
  }, [clinicId]);

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-white">
        <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-3 bg-white px-6">
        <AlertTriangle className="h-8 w-8 text-amber-500" />
        <p className="text-slate-700 font-medium text-center">{error ?? "Política de privacidade não encontrada."}</p>
      </div>
    );
  }

  const name  = data.clinic_legal_name  || data.clinic_name;
  const cnpj  = data.clinic_cnpj        || "Não informado";
  const cont  = data.clinic_contact     || "Não informado";
  const dpo   = data.dpo_name           || "Encarregado de Dados (DPO)";
  const dpoC  = data.dpo_contact        || "Não informado";

  return (
    <div className="min-h-screen bg-white text-slate-800">
      {/* Header */}
      <header className="border-b border-slate-200 bg-slate-50 px-6 py-4">
        <div className="max-w-3xl mx-auto flex items-center gap-3">
          <Shield className="h-5 w-5 text-violet-600 shrink-0" />
          <div>
            <p className="text-xs text-slate-500 uppercase tracking-wide font-medium">Política de Privacidade</p>
            <p className="text-sm font-semibold text-slate-800">{name}</p>
          </div>
        </div>
      </header>

      {/* Body */}
      <main className="max-w-3xl mx-auto px-6 py-10 space-y-8 text-[15px] leading-relaxed">

        <h1 className="text-2xl font-bold text-slate-900">
          Política de Privacidade do Atendimento por Chatbot
        </h1>
        <p className="text-slate-600">A sua privacidade é importante para nós.</p>
        <p>
          Este chatbot é utilizado para facilitar o atendimento da clínica, incluindo dúvidas iniciais,
          informações sobre serviços, encaminhamento para equipe humana, agendamentos, confirmações de
          contato e registro do histórico de atendimento.
        </p>
        <p>
          Ao utilizar este canal, seus dados pessoais poderão ser tratados para viabilizar o atendimento
          solicitado, melhorar a comunicação com a clínica, cumprir obrigações legais e regulatórias e manter
          a segurança das interações, nos termos da Lei Geral de Proteção de Dados Pessoais, Lei nº 13.709/2018.
        </p>

        <Section title="1. Quem são os responsáveis pelo tratamento dos dados">
          <p>
            O controlador dos seus dados pessoais é a clínica responsável por este atendimento:
          </p>
          <ul className="mt-3 space-y-1 list-none pl-0">
            <li><strong>{name}</strong></li>
            <li>CNPJ: <strong>{cnpj}</strong></li>
            <li>Contato: <strong>{cont}</strong></li>
          </ul>
          <p className="mt-3">
            A NeuroStack atua como provedora da tecnologia utilizada neste chatbot, podendo operar dados
            pessoais em nome da clínica para viabilizar o funcionamento da solução, conforme as instruções da
            própria clínica e os limites da legislação aplicável. A LGPD diferencia os papéis de controlador
            e operador no tratamento de dados pessoais.
          </p>
        </Section>

        <Section title="2. Quais dados podem ser tratados">
          <p>Dependendo da sua interação, este canal pode tratar dados como:</p>
          <ul className="mt-2 space-y-1 list-disc pl-5 text-slate-700">
            {["nome","telefone","e-mail","CPF, quando necessário para identificação ou agendamento",
              "informações fornecidas por você durante a conversa",
              "dados de atendimento, como data, horário e unidade",
              "histórico de mensagens trocadas neste canal"].map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <p className="mt-3">
            Em alguns casos, especialmente em contextos de saúde, podem ser tratados dados pessoais sensíveis,
            como informações relacionadas à sua saúde, quando isso for necessário para o atendimento solicitado.
            A LGPD trata dados de saúde como dados pessoais sensíveis, sujeitos a proteção reforçada.
          </p>
        </Section>

        <Section title="3. Para quais finalidades usamos os dados">
          <ul className="mt-2 space-y-1 list-disc pl-5 text-slate-700">
            {["realizar atendimento inicial e responder suas solicitações",
              "direcionar você para a equipe da clínica",
              "realizar ou apoiar agendamentos, confirmações e remarcações",
              "registrar o histórico do atendimento",
              "melhorar a qualidade, segurança e organização do serviço",
              "cumprir obrigações legais, regulatórias e de auditoria",
              "prevenir fraudes, abusos e uso indevido do canal"].map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Section>

        <Section title="4. Com quem os dados podem ser compartilhados">
          <ul className="mt-2 space-y-1 list-disc pl-5 text-slate-700">
            {["profissionais e equipes autorizadas da própria clínica",
              "prestadores de tecnologia e hospedagem envolvidos na operação do chatbot",
              "sistemas integrados da clínica, como agenda, CRM ou prontuário, quando aplicável",
              "autoridades públicas, quando houver obrigação legal ou regulatória"].map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <p className="mt-3">
            O compartilhamento será realizado apenas quando necessário para as finalidades informadas e dentro
            dos limites da legislação aplicável.
          </p>
        </Section>

        <Section title="5. Por quanto tempo os dados são armazenados">
          <p>
            Os dados serão armazenados pelo tempo necessário para cumprir as finalidades deste atendimento,
            atender exigências legais, regulatórias e contratuais e resguardar direitos da clínica, do
            paciente e dos envolvidos no atendimento.
          </p>
          <p className="mt-3">
            Quando possível e aplicável, os dados serão eliminados, anonimizados ou mantidos de forma segura
            conforme os prazos legais e a necessidade operacional.
          </p>
        </Section>

        <Section title="6. Como protegemos os dados">
          <p>
            Adotamos medidas técnicas e organizacionais de segurança para proteger os dados pessoais contra
            acessos não autorizados, perda, alteração, divulgação indevida ou qualquer forma de tratamento
            inadequado.
          </p>
          <p className="mt-3">
            Ainda assim, nenhum sistema é totalmente livre de riscos. Caso ocorra incidente de segurança com
            risco ou dano relevante, as medidas cabíveis serão adotadas conforme a legislação.
          </p>
        </Section>

        <Section title="7. Seus direitos como titular de dados">
          <p>Nos termos da LGPD, você pode solicitar, quando aplicável:</p>
          <ul className="mt-2 space-y-1 list-disc pl-5 text-slate-700">
            {["confirmação da existência de tratamento",
              "acesso aos seus dados",
              "correção de dados incompletos, inexatos ou desatualizados",
              "anonimização, bloqueio ou eliminação de dados desnecessários, excessivos ou tratados de forma inadequada",
              "portabilidade, quando cabível",
              "informação sobre compartilhamentos realizados",
              "revisão de decisões, quando aplicável",
              "informações sobre a possibilidade de não fornecer consentimento, quando essa base legal for utilizada",
              "revogação do consentimento, quando o tratamento depender dele"].map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </Section>

        <Section title="8. Como exercer seus direitos ou tirar dúvidas">
          <p>Para solicitações relacionadas aos seus dados pessoais, entre em contato com:</p>
          <ul className="mt-3 space-y-1 list-none pl-0">
            <li><strong>{dpo}</strong></li>
            <li>Contato/E-mail: <strong>{dpoC}</strong></li>
          </ul>
        </Section>

        <Section title="9. Sobre o uso deste canal">
          <p>
            Este chatbot é um canal de apoio ao atendimento. Em situações específicas, você poderá ser
            direcionado para um atendente humano da clínica.
          </p>
          <p className="mt-3">
            Recomendamos que você não envie, por este canal, informações desnecessárias ou excessivas. Caso
            precise de atendimento clínico urgente, procure imediatamente o canal adequado da clínica ou o
            serviço de urgência mais próximo.
          </p>
        </Section>

        <Section title="10. Atualizações desta política">
          <p>
            Esta Política de Privacidade poderá ser atualizada a qualquer momento para refletir melhorias no
            serviço, mudanças operacionais ou exigências legais.
          </p>
          <p className="mt-3 font-medium text-slate-700">
            Última atualização: <strong>{today()}</strong>
          </p>
        </Section>

        {data.slm_training_consent && (
          <Section title="Adendo Especial: Uso de Dados Anonimizados para Treinamento de IA (SLM)">
            <p>
              Com o objetivo exclusivo de especializar o atendimento, aumentar a precisão das respostas
              médicas/odontológicas e reduzir os custos operacionais da plataforma, os dados de interações
              e históricos de conversas poderão ser utilizados para o treinamento, ajuste fino (fine-tuning)
              e calibração de Modelos de Linguagem (Small Language Models - SLMs) e motores cognitivos de
              inteligência artificial.
            </p>
            <p className="mt-3">
              <strong>Garantia de Anonimização Absoluta:</strong> Para que ocorra este processo sem qualquer
              risco à sua privacidade, todos os dados transitados passam por um rigoroso processo de
              descaracterização e anonimização irreversível. Isso significa que nomes, números de telefone,
              CPFs, documentos, fotos e qualquer outra informação que possa identificar direta ou
              indiretamente o paciente são sumariamente removidos antes de qualquer processamento por estes
              motores de IA.
            </p>
            <p className="mt-3">
              Conforme o artigo 12 da Lei Geral de Proteção de Dados (LGPD - Lei nº 13.709/18), os dados
              anonimizados perdem a natureza de dados pessoais para fins legais. Dessa forma, garantimos que
              o seu histórico de atendimento seja tratado puramente como um padrão estatístico e de
              linguagem, totalmente desvinculado da sua identidade e focado apenas na evolução contínua da
              qualidade do atendimento em saúde.
            </p>
          </Section>
        )}

        {/* Footer */}
        <footer className="pt-8 border-t border-slate-200 text-xs text-slate-400 flex items-center gap-2">
          <Shield className="h-3.5 w-3.5 text-violet-400 shrink-0" />
          <span>
            Página gerada pela plataforma <strong className="text-slate-500">NeuroStack</strong>, infraestrutura cognitiva para agentes de IA em saúde.
          </span>
        </footer>
      </main>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h2 className="text-lg font-semibold text-slate-900 border-b border-slate-100 pb-1">{title}</h2>
      <div className="text-slate-700">{children}</div>
    </section>
  );
}
