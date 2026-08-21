import en_common from './en/common';
import en_app from './en/app';
import en_sidebar from './en/sidebar';
import en_workspace from './en/workspace';
import en_conversation from './en/conversation';
import en_status from './en/status';
import en_composer from './en/composer';
import en_model from './en/model';
import en_approval from './en/approval';
import en_question from './en/question';
import en_tasks from './en/tasks';
import en_thinking from './en/thinking';
import en_diff from './en/diff';
import en_filePreview from './en/filePreview';
import en_mention from './en/mention';
import en_commands from './en/commands';
import en_tools from './en/tools';
import en_layout from './en/layout';
import en_mobile from './en/mobile';

import zh_common from './zh/common';
import zh_app from './zh/app';
import zh_sidebar from './zh/sidebar';
import zh_workspace from './zh/workspace';
import zh_conversation from './zh/conversation';
import zh_status from './zh/status';
import zh_composer from './zh/composer';
import zh_model from './zh/model';
import zh_approval from './zh/approval';
import zh_question from './zh/question';
import zh_tasks from './zh/tasks';
import zh_thinking from './zh/thinking';
import zh_diff from './zh/diff';
import zh_filePreview from './zh/filePreview';
import zh_mention from './zh/mention';
import zh_commands from './zh/commands';
import zh_tools from './zh/tools';
import zh_layout from './zh/layout';
import zh_mobile from './zh/mobile';
import en_settings from './en/settings';
import zh_settings from './zh/settings';
import en_header from './en/header';
import zh_header from './zh/header';
import en_focus from './en/focus';
import zh_focus from './zh/focus';

export const messages = {
  en: {
    common: en_common,
    app: en_app,
    sidebar: en_sidebar,
    workspace: en_workspace,
    conversation: en_conversation,
    status: en_status,
    composer: en_composer,
    model: en_model,
    approval: en_approval,
    question: en_question,
    tasks: en_tasks,
    thinking: en_thinking,
    diff: en_diff,
    filePreview: en_filePreview,
    mention: en_mention,
    commands: en_commands,
    tools: en_tools,
    layout: en_layout,
    mobile: en_mobile,
    settings: en_settings,
    header: en_header,
    focus: en_focus,
  },
  zh: {
    common: zh_common,
    app: zh_app,
    sidebar: zh_sidebar,
    workspace: zh_workspace,
    conversation: zh_conversation,
    status: zh_status,
    composer: zh_composer,
    model: zh_model,
    approval: zh_approval,
    question: zh_question,
    tasks: zh_tasks,
    thinking: zh_thinking,
    diff: zh_diff,
    filePreview: zh_filePreview,
    mention: zh_mention,
    commands: zh_commands,
    tools: zh_tools,
    layout: zh_layout,
    mobile: zh_mobile,
    settings: zh_settings,
    header: zh_header,
    focus: zh_focus,
  },
} as const;

export default messages;
