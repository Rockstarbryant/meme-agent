import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DataLabel, ModeBadge, SimulatedLabel } from "@/components/badges";
import { ModeBanner } from "@/components/status-banner";
import { agent } from "./fixtures";

describe("PAPER / LIVE / DEMO labelling", () => {
  it("shows PAPER MODE and DEMO DATA, and no emergency banner by default", () => {
    render(<ModeBanner status={agent()} connected />);
    expect(screen.getByText("PAPER MODE")).toBeInTheDocument();
    expect(screen.getByText("DEMO DATA")).toBeInTheDocument();
    expect(screen.getByText("AGENT STOPPED")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("live")).toBeInTheDocument();
    expect(screen.getByText("RUNNER ONLINE")).toBeInTheDocument();
  });

  it("makes a missing or offline runner obvious", () => {
    const { unmount } = render(<ModeBanner status={agent({ runner: null, state: "OFFLINE" })} connected />);
    expect(screen.getByText("NO RUNNER")).toBeInTheDocument();
    expect(screen.getByText("AGENT OFFLINE")).toBeInTheDocument();
    unmount();
    render(<ModeBanner status={agent({ state: "OFFLINE", runner: { ...agent().runner!, online: false } })} connected />);
    expect(screen.getByText("RUNNER OFFLINE")).toBeInTheDocument();
  });

  it("shows LIVE MODE distinctly and omits the demo badge for real data", () => {
    render(<ModeBanner status={agent({ mode: "LIVE", data_source: "arc", state: "RUNNING", desired_state: "RUNNING" })} connected={false} />);
    expect(screen.getByText("LIVE MODE")).toBeInTheDocument();
    expect(screen.queryByText("PAPER MODE")).not.toBeInTheDocument();
    expect(screen.queryByText("DEMO DATA")).not.toBeInTheDocument();
    expect(screen.getByText("AGENT RUNNING")).toBeInTheDocument();
    expect(screen.getByText("offline")).toBeInTheDocument();
  });

  it("makes an emergency stop impossible to miss and says positions stay protected", () => {
    render(<ModeBanner status={agent({ emergency_stop: true })} connected />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("EMERGENCY STOP ACTIVE");
    expect(alert).toHaveTextContent("Existing positions stay protected");
  });

  it("labels simulated fills and never mislabels demo data as live", () => {
    render(<><ModeBadge mode="PAPER" /><DataLabel label="DEMO DATA" /><SimulatedLabel simulated /><DataLabel label="LIVE DATA" /></>);
    expect(screen.getByText("PAPER (SIMULATED)")).toBeInTheDocument();
    expect(screen.getByText("DEMO DATA")).toBeInTheDocument();
    expect(screen.getByText("LIVE DATA")).toBeInTheDocument();
  });
});
