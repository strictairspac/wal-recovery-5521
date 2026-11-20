import {HANDOVER_STATUSES} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';
export const handoverBadge=(status)=>`<span class="badge handover ${esc(status)}">${esc(HANDOVER_STATUSES[status]||status)}</span>`;