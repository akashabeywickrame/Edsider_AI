import type { ReactNode } from "react";

/** Bands from src/dedupe/pipeline.py plus recorded verdicts. */
export type VerdictName = "LIKELY DUPLICATE" | "NEEDS REVIEW" | "DISTINCT" | "REPEAT ON ASSESSMENT" | "EXACT MATCH" | "DUPLICATE" | "NOT A DUPLICATE";
export type PaperStatus = "uploaded" | "extracted" | "checked" | "teacher_review" | "refer_back" | "review_done" | "published";
export type GuardReason = string | { guard: "Numbers differ" | "Opposing concepts" | "Different named entities" | "Grade or subject mismatch" | "Shared passage" | string; text: string };

export interface ButtonProps { variant?: "primary" | "secondary" | "ghost" | "danger"; size?: "md" | "sm"; disabled?: boolean; /** trailing arrow that slides on hover, as on the site CTA */ arrow?: boolean; type?: "button" | "submit"; onClick?: () => void; children: ReactNode; }
export interface VerdictProps { verdict?: VerdictName; /** used when verdict is omitted */ score?: number; children?: ReactNode; }
export interface StatusPillProps { status: PaperStatus; children?: ReactNode; }
export interface ScoreProps { /** 0-1, after guards */ score: number; /** 0-1, before guards */ raw?: number; thresholds?: number[]; min?: number; legend?: boolean; }
export interface GuardNoteProps { reasons: GuardReason[]; raw?: number; score?: number; }
export interface NoticeProps { tone?: "info" | "success" | "warning" | "error" | "guard"; title?: string; children: ReactNode; }
export interface QuestionCardProps { id: number; text: string; grade?: string; subject?: string; topic?: string; number?: number; contributor?: string; assessments?: number[]; lang?: "en" | "si" | "ta"; badge?: ReactNode; }
export interface DecisionBarProps { onConfirm?: () => void; onDistinct?: () => void; onSkip?: () => void; hint?: string; }
export interface PairReviewProps extends DecisionBarProps {
  score: number; raw?: number; verdict?: VerdictName; context?: string;
  a: QuestionCardProps; b: QuestionCardProps; reasons?: GuardReason[];
  decision?: { verdict: string; by: string; at: string }; passageNote?: string; readOnly?: boolean;
}
export interface McqOption { label: string; text: string; }
export interface McqOptionsProps { options: McqOption[]; correct?: string; }
export interface McqEditorProps { options: McqOption[]; /** correct label(s), comma-separated */ correct?: string; /** start with "More than one" correct */ many?: boolean; }
export interface DraftCardProps { /** MCQ: start the option editor in "More than one" mode */ many?: boolean; label: string; text: string; marks?: number; depth?: 0 | 1 | 2; kind?: "question" | "stem"; options?: McqOption[]; correct?: string; answer?: string; reviewed?: boolean; editable?: boolean; onRemove?: false | (() => void); }
export type IconName = "search" | "copy" | "paper" | "check" | "upload" | "bank" | "plus" | "users";
export interface StatProps { label: string; value: string; icon?: IconName; accent?: "primary" | "coral" | "gold" | "blue"; }
export interface LanguagePillProps { lang: "si" | "ta" | "en"; children?: ReactNode; }
export interface PageHeaderProps { title: string; accent?: string; subtitle?: string; children?: ReactNode; }
export interface TopNavProps { pages: string[]; active?: string; title?: string; who?: string; }
export interface SectionRailProps { items: { id: string; icon: IconName; label: string }[]; active?: string; label?: string; }
export interface FieldProps { label: string; as?: "input" | "textarea" | "select"; options?: string[]; placeholder?: string; value?: string; help?: string; rows?: number; id?: string; type?: string; }
export interface ToggleProps { label: string; checked?: boolean; }

export declare function Button(p: ButtonProps): JSX.Element;
export declare function Verdict(p: VerdictProps): JSX.Element;
export declare function StatusPill(p: StatusPillProps): JSX.Element;
export declare function Score(p: ScoreProps): JSX.Element;
export declare function GuardNote(p: GuardNoteProps): JSX.Element | null;
export declare function Notice(p: NoticeProps): JSX.Element;
export declare function QuestionCard(p: QuestionCardProps): JSX.Element;
export declare function DecisionBar(p: DecisionBarProps): JSX.Element;
export declare function PairReview(p: PairReviewProps): JSX.Element;
export declare function McqOptions(p: McqOptionsProps): JSX.Element;
export declare function McqEditor(p: McqEditorProps): JSX.Element;
export declare function DraftCard(p: DraftCardProps): JSX.Element;
export declare function Stat(p: StatProps): JSX.Element;
export declare function Field(p: FieldProps): JSX.Element;
export declare function Toggle(p: ToggleProps): JSX.Element;
export declare function LanguagePill(p: LanguagePillProps): JSX.Element;
export declare function PageHeader(p: PageHeaderProps): JSX.Element;
export declare function TopNav(p: TopNavProps): JSX.Element;
export declare function SectionRail(p: SectionRailProps): JSX.Element;
export declare function Icon(p: { name: IconName; size?: number }): JSX.Element;
/** 0.92+ LIKELY DUPLICATE, 0.85+ NEEDS REVIEW, else DISTINCT */
export declare function band(score: number): VerdictName;
