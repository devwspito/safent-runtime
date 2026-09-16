const UNIT_LABEL = {
    bytes: 'MB',
    layers: 'partes',
    steps: 'pasos',
};
function toDisplayNumber(value, unit) {
    return unit === 'bytes' ? value / (1024 * 1024) : value;
}
/** "932 de 932 MB" / "3 de 5 partes" — no infrastructure vocabulary. */
export function formatProgress(stage) {
    if (stage.done === undefined)
        return undefined;
    const unit = stage.unit ?? 'bytes';
    const done = Math.round(toDisplayNumber(stage.done, unit));
    if (stage.total === undefined)
        return `${done} ${UNIT_LABEL[unit]}`;
    const total = Math.round(toDisplayNumber(stage.total, unit));
    return `${done} de ${total} ${UNIT_LABEL[unit]}`;
}
/** 0-100, or undefined when the stage has no known total (indeterminate). */
export function progressPercent(stage) {
    if (stage.done === undefined || !stage.total)
        return undefined;
    return Math.max(0, Math.min(100, Math.round((stage.done / stage.total) * 100)));
}
//# sourceMappingURL=format.js.map