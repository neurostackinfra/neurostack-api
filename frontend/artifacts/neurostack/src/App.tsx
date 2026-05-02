import { Switch, Route, Router as WouterRouter } from "wouter";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider, useAuth } from "@/context/auth";
import { I18nProvider } from "@/context/i18n";
import { DemoLeadGate } from "@/components/DemoLeadGate";
import NotFound from "@/pages/not-found";
import Login from "@/pages/login";
import { Layout } from "@/components/layout";
import Dashboard from "@/pages/dashboard";
import Chat from "@/pages/chat";
import TracesList from "@/pages/traces";
import TraceDetail from "@/pages/trace-detail";
import ClinicConfig from "@/pages/clinic-config";
import Agents from "@/pages/agents";
import CRMIntegration from "@/pages/crm";
import KnowledgeBase from "@/pages/knowledge-base";
import MasterAdmin from "@/pages/master-admin";
import Onboarding from "@/pages/onboarding";
import Seguranca from "@/pages/seguranca";
import SlmCortex from "@/pages/slm-cortex";
import Consultoria from "@/pages/consultoria";
import Midias from "@/pages/midias";
import Billing from "@/pages/billing";
import VoiceAI from "@/pages/voice-ai";
import Suporte from "@/pages/suporte";
import PortalParceiros from "@/pages/portal-parceiros";
import RlhfAudit from "@/pages/rlhf-audit";
import Privacidade from "@/pages/privacidade";
import BuilderPanel from "@/pages/builder";

const queryClient = new QueryClient();

function Router() {
  const { isAuthenticated, isSuperAdmin, isInvestor, leadSubmitted } = useAuth();

  return (
    <Switch>
      <Route path="/privacidade/:clinicId" component={Privacidade} />
      <Route component={AuthenticatedRouter} />
    </Switch>
  );
}

function AuthenticatedRouter() {
  const { isAuthenticated, isSuperAdmin, isInvestor, isBuilder, leadSubmitted } = useAuth();

  if (!isAuthenticated) {
    return <Login />;
  }

  // Demo user must fill the lead gate before accessing any page
  if (isInvestor && !leadSubmitted) {
    return <DemoLeadGate />;
  }

  return (
    <Layout>
      <Switch>
        <Route path="/" component={Chat} />
        <Route path="/dashboard" component={Dashboard} />
        <Route path="/traces" component={TracesList} />
        <Route path="/traces/:id" component={TraceDetail} />
        <Route path="/config" component={ClinicConfig} />
        <Route path="/agents" component={Agents} />
        <Route path="/crm" component={CRMIntegration} />
        <Route path="/knowledge-base" component={KnowledgeBase} />
        {(isSuperAdmin || isInvestor) && !isBuilder && <Route path="/master-admin" component={MasterAdmin} />}
        {(isSuperAdmin || isInvestor) && !isBuilder && <Route path="/rlhf-audit" component={RlhfAudit} />}
        {isBuilder && <Route path="/builder" component={BuilderPanel} />}
        <Route path="/onboarding" component={Onboarding} />
        <Route path="/seguranca" component={Seguranca} />
        <Route path="/slm-cortex" component={SlmCortex} />
        <Route path="/consultoria" component={Consultoria} />
        <Route path="/midias" component={Midias} />
        <Route path="/billing" component={Billing} />
        <Route path="/voice-ai" component={VoiceAI} />
        <Route path="/suporte" component={Suporte} />
        <Route path="/portal-parceiros" component={PortalParceiros} />
        <Route component={NotFound} />
      </Switch>
    </Layout>
  );
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <I18nProvider>
          <AuthProvider>
            <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, "")}>
              <Router />
            </WouterRouter>
            <Toaster />
          </AuthProvider>
        </I18nProvider>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
