import {
  createContext,
  useContext,
  useState,
  useCallback,
  type ReactNode,
} from "react";
import { useListClinics, type ClinicListItem } from "@workspace/api-client-react";

interface TenantContextValue {
  activeClinicId: number;
  setActiveClinicId: (id: number) => void;
  clinics: ClinicListItem[];
  isLoadingClinics: boolean;
  activeClinic: ClinicListItem | undefined;
}

const LS_KEY = "ns_active_clinic";

function loadClinicId(): number {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return 1;
    const n = Number(raw);
    return isNaN(n) ? 1 : n;
  } catch {
    return 1;
  }
}

const TenantContext = createContext<TenantContextValue | null>(null);

export function TenantProvider({ children }: { children: ReactNode }) {
  const [activeClinicId, setActiveClinicIdState] = useState<number>(loadClinicId);
  const { data: clinics = [], isLoading: isLoadingClinics } = useListClinics();

  const setActiveClinicId = useCallback((id: number) => {
    localStorage.setItem(LS_KEY, String(id));
    setActiveClinicIdState(id);
  }, []);

  const activeClinic = clinics.find((c) => c.clinic_id === activeClinicId);

  return (
    <TenantContext.Provider
      value={{
        activeClinicId,
        setActiveClinicId,
        clinics,
        isLoadingClinics,
        activeClinic,
      }}
    >
      {children}
    </TenantContext.Provider>
  );
}

export function useTenant() {
  const ctx = useContext(TenantContext);
  if (!ctx) throw new Error("useTenant must be inside TenantProvider");
  return ctx;
}
