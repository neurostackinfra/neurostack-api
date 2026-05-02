import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import { jwtDecode } from "jwt-decode";
import { setAuthTokenGetter } from "@workspace/api-client-react";

interface JwtPayload {
  sub?: string;
  email?: string;
  tenant_id?: string;
  clinic_id?: string;
  user_id?: string;
  builder_id?: string;
  role?: string;
  exp?: number;
}

interface AuthState {
  token: string | null;
  email: string | null;
  clinicId: string | null;
  tenantId: string | null;
  userId: string | null;
  builderId: string | null;
  role: string | null;
  viewingClinicId: string | null;
  leadSubmitted: boolean;
}

interface LoginResponse {
  token: string;
  email: string;
  tenant_id: string;
  clinic_id: string;
  role?: string;
}

interface AuthContextValue {
  isAuthenticated: boolean;
  email: string | null;
  clinicId: string;
  tenantId: string;
  userId: string;
  builderId: string | null;
  role: string;
  isSuperAdmin: boolean;
  isInvestor: boolean;
  isBuilder: boolean;
  isReadOnly: boolean;
  leadSubmitted: boolean;
  viewingClinicId: string;
  setViewingClinicId: (id: string) => void;
  setLeadSubmitted: () => void;
  login: (email: string, password: string) => Promise<void>;
  loginWithToken: (token: string, email: string, clinicId: string, role?: string) => void;
  logout: () => void;
}

const LS_KEY = "ns_auth";

function loadAuth(): AuthState {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return { token: null, email: null, clinicId: null, tenantId: null, userId: null, builderId: null, role: null, viewingClinicId: null, leadSubmitted: false };
    const parsed = JSON.parse(raw) as AuthState;
    // Check token expiration
    if (parsed.token) {
      try {
        const decoded = jwtDecode<JwtPayload>(parsed.token);
        if (decoded.exp && decoded.exp * 1000 < Date.now()) {
          localStorage.removeItem(LS_KEY);
          return { token: null, email: null, clinicId: null, tenantId: null, userId: null, builderId: null, role: null, viewingClinicId: null, leadSubmitted: false };
        }
      } catch {
        // If token can't be decoded, clear it
        localStorage.removeItem(LS_KEY);
        return { token: null, email: null, clinicId: null, tenantId: null, userId: null, builderId: null, role: null, viewingClinicId: null, leadSubmitted: false };
      }
    }
    return { ...parsed, leadSubmitted: parsed.leadSubmitted ?? false };
  } catch {
    return { token: null, email: null, clinicId: null, tenantId: null, userId: null, builderId: null, role: null, viewingClinicId: null, leadSubmitted: false };
  }
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>(loadAuth);

  // Configure the custom fetch auth token getter
  useEffect(() => {
    setAuthTokenGetter(() => {
      try {
        const raw = localStorage.getItem(LS_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw) as AuthState;
        return parsed.token ?? null;
      } catch {
        return null;
      }
    });

    return () => {
      setAuthTokenGetter(null);
    };
  }, []);

  const applyToken = useCallback((token: string, email: string, clinicId: string, role = "clinic_admin") => {
    // Decode JWT to extract additional fields
    let tenantId: string | null = null;
    let userId: string | null = null;
    let builderId: string | null = null;
    try {
      const decoded = jwtDecode<JwtPayload>(token);
      tenantId = decoded.tenant_id ?? null;
      userId = decoded.user_id ?? decoded.sub ?? null;
      builderId = decoded.builder_id ?? null;
      if (!email && decoded.email) email = decoded.email;
      if (!role && decoded.role) role = decoded.role;
      if (!clinicId && decoded.clinic_id) clinicId = decoded.clinic_id;
    } catch {
      // JWT decode failed — proceed with provided values
    }

    const next: AuthState = {
      token,
      email,
      clinicId,
      tenantId,
      userId,
      builderId,
      role,
      viewingClinicId: clinicId,
      leadSubmitted: false,
    };
    localStorage.setItem(LS_KEY, JSON.stringify(next));
    setState(next);
  }, []);

  // login(email, password) — authenticates against POST /v1/auth/login, stores JWT
  const login = useCallback(async (email: string, password: string): Promise<void> => {
    const resp = await fetch("/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    if (!resp.ok) {
      throw new Error(`Login failed: ${resp.status}`);
    }

    const data = (await resp.json()) as LoginResponse;
    applyToken(data.token, data.email, data.clinic_id, data.role ?? "clinic_admin");
  }, [applyToken]);

  // loginWithToken — for direct token-based login (backward compat)
  const loginWithToken = useCallback((token: string, email: string, clinicId: string, role = "clinic_admin") => {
    applyToken(token, email, clinicId, role);
  }, [applyToken]);

  const logout = useCallback(() => {
    localStorage.removeItem(LS_KEY);
    setState({ token: null, email: null, clinicId: null, tenantId: null, userId: null, builderId: null, role: null, viewingClinicId: null, leadSubmitted: false });
  }, []);

  const setViewingClinicId = useCallback((id: string) => {
    setState((prev) => {
      const next = { ...prev, viewingClinicId: id };
      localStorage.setItem(LS_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const setLeadSubmitted = useCallback(() => {
    setState((prev) => {
      const next = { ...prev, leadSubmitted: true };
      localStorage.setItem(LS_KEY, JSON.stringify(next));
      return next;
    });
  }, []);

  const role         = state.role ?? "clinic_admin";
  const isSuperAdmin = role === "super_admin";
  const isInvestor   = role === "investor";
  const isBuilder    = role === "builder";
  const builderId    = state.builderId ?? null;
  const clinicId     = state.clinicId ?? "";
  const tenantId     = state.tenantId ?? "";
  const userId       = state.userId ?? "";
  const viewingClinicId = state.viewingClinicId ?? clinicId;

  return (
    <AuthContext.Provider
      value={{
        isAuthenticated: !!state.token,
        email: state.email,
        clinicId,
        tenantId,
        userId,
        builderId,
        role,
        isSuperAdmin,
        isInvestor,
        isBuilder,
        isReadOnly: isInvestor,
        leadSubmitted: state.leadSubmitted,
        viewingClinicId,
        setViewingClinicId,
        setLeadSubmitted,
        login,
        loginWithToken,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be inside AuthProvider");
  return ctx;
}
