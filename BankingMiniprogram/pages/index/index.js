const SENSITIVE_KINDS = ["transfer", "wealth", "cross_lock", "cross_release"];

function clipSpeech(text) {
  const raw = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  if (!raw) return "";
  return raw.length > 180 ? raw.slice(0, 180) : raw;
}

// 把文字回复和卡片里的关键字段拼成一段可朗读的话
function speechFromMessage(msg) {
  if (!msg || msg.role !== "assistant" || msg.type === "WELCOME_CARD") return "";
  const parts = [];
  if (msg.text) parts.push(String(msg.text));
  const card = msg.cardData || {};
  if (card.title && card.title !== msg.text) parts.push(String(card.title));
  (card.highlights || []).forEach((line) => {
    if (line) parts.push(String(line));
  });
  (card.sections || []).forEach((sec) => {
    if (sec && sec.label) parts.push(`${sec.label}：${sec.value || ""}`);
  });
  (card.items || []).forEach((row) => {
    if (!row) return;
    parts.push([row.title, row.value, row.meta].filter(Boolean).join(" "));
  });
  return clipSpeech(parts.join("。"));
}

// 后端服务地址：全项目唯一入口，换部署环境只改这一行。
// 本机联调用 127.0.0.1；真机/体验版必须换成已备案的 HTTPS 域名
// （微信要求 request 域名为 https、不能是 IP、不能带端口，并需在公众平台配置服务器域名）。
const API_BASE = "http://127.0.0.1:8001";
// 放在页面外面，避免 setData 还没写上时，回复回来却判断成「语音关」
let voiceOnFlag = false;

function welcomeMessages() {
  return [{ role: "assistant", type: "WELCOME_CARD" }];
}

Page({
  data: {
    loggedIn: false,
    userId: "",
    userName: "",
    phoneMasked: "",
    authToken: "",
    loginAccount: "",
    loginPassword: "",
    loginError: "",
    balance: "--",
    // 2026-10-06 新增：界面模式（normal 普通版 / elder 适老版）
    uiMode: "normal",
    // 2026-10-08 新增：适老版语音播报 + 亲情守护
    voiceOn: false,
    speaking: false,
    speechSrc: "",
    guardian: null,
    guardianForm: {
      name: "",
      phone: "",
      relation: "女儿"
    },
    inputContent: "",
    // 2026-10-23 新增：请求进行中标记，用于内联「正在输入」气泡与防重复发送
    sending: false,
    lastMsgId: "msg-0",
    messages: welcomeMessages(),
    purchaseSheetVisible: false,
    purchaseForm: {
      productId: "",
      productName: "",
      minAmount: 0,
      amountInput: ""
    },
    purchaseAmountError: "",
    purchaseCanSubmit: false,
    pinSheet: {
      visible: false,
      orderId: "",
      msgIndex: -1,
      title: "请输入交易密码",
      summary: "",
      password: "",
      error: "",
      fraud: null,
      riskAck: false
    }
  },

  onLoad() {
    const savedMode = wx.getStorageSync("ui_mode");
    if (savedMode === "elder" || savedMode === "normal") {
      this.setData({ uiMode: savedMode });
    }
    // 语音开关同时记在页面实例上。只信 this.data 时，界面已经是「语音开」，自动朗读仍会判断成关。
    const savedVoice = wx.getStorageSync("voice_on");
    voiceOnFlag = savedVoice === true || savedVoice === "1" || savedVoice === 1;
    this._voiceOn = voiceOnFlag;
    this.setData({ voiceOn: voiceOnFlag });
    const session = wx.getStorageSync("bank_session");
    if (session && session.userId && session.token) {
      this.applySession(session, false);
    }
  },

  onShow() {
    if (this.data.loggedIn) {
      this.syncRealBalance();
      this.startNoticePoll();
    }
  },

  onHide() {
    if (this._noticeTimer) {
      clearInterval(this._noticeTimer);
      this._noticeTimer = null;
    }
  },

  startNoticePoll() {
    this.pullScenarioNotices();
    if (this._noticeTimer) clearInterval(this._noticeTimer);
    this._noticeTimer = setInterval(() => this.pullScenarioNotices(), 30000);
  },

  pullScenarioNotices() {
    if (!this.data.loggedIn || !this.data.authToken || !this.data.userId) return;
    wx.request({
      url: `${API_BASE}/api/scenario/notices?user_id=${encodeURIComponent(this.data.userId)}&token=${encodeURIComponent(this.data.authToken)}`,
      success: (res) => {
        if (res.statusCode === 401 || !res.data || !res.data.success) return;
        const notices = res.data.notices || [];
        if (!notices.length) return;
        const known = {};
        (this.data.messages || []).forEach((item) => {
          if (item.noticeId) known[item.noticeId] = true;
        });
        const fresh = notices.filter((item) => !known[item.notice_id]);
        if (fresh.length) {
          const messages = this.data.messages.slice();
          fresh.forEach((item) => {
            messages.push({
              role: "assistant",
              type: "TEXT",
              text: `${item.title}：${item.content}`,
              noticeId: item.notice_id
            });
          });
          this.setData({
            messages,
            lastMsgId: `msg-${messages.length - 1}`
          });
          this.syncRealBalance();
          this.maybeSpeak(fresh[fresh.length - 1].content);
        }
        wx.request({
          url: `${API_BASE}/api/scenario/notices/read`,
          method: "POST",
          header: { "content-type": "application/json" },
          data: {
            user_id: this.data.userId,
            token: this.data.authToken,
            notice_ids: notices.map((item) => item.notice_id)
          }
        });
      }
    });
  },

  onLoginAccount(e) {
    this.setData({ loginAccount: e.detail.value, loginError: "" });
  },

  onLoginPassword(e) {
    this.setData({ loginPassword: e.detail.value, loginError: "" });
  },

  submitLogin() {
    const account = (this.data.loginAccount || "").trim();
    const password = this.data.loginPassword || "";
    if (!account || !password) {
      this.setData({ loginError: "请输入账号和密码" });
      return;
    }
    wx.showLoading({ title: "登录中..." });
    wx.request({
      url: `${API_BASE}/api/auth/login`,
      method: "POST",
      data: { account, password },
      header: { "content-type": "application/json" },
      success: (res) => {
        wx.hideLoading();
        const body = res.data || {};
        if (res.statusCode >= 400 || !body.success) {
          const detail = body.detail || "账号或密码错误";
          this.setData({ loginError: typeof detail === "string" ? detail : "账号或密码错误" });
          return;
        }
        this.applySession({
          userId: body.user_id,
          userName: body.name,
          phoneMasked: body.phone_masked,
          token: body.token,
          balance: body.balance,
          silver: body.silver,
          guardian: body.guardian || null
        }, true);
      },
      fail: () => {
        wx.hideLoading();
        this.setData({ loginError: "无法连接银行服务，请确认后端已启动" });
      }
    });
  },

  applySession(session, resetChat) {
    const next = {
      loggedIn: true,
      userId: session.userId,
      userName: session.userName,
      phoneMasked: session.phoneMasked,
      authToken: session.token,
      balance: session.balance !== undefined ? session.balance : this.data.balance,
      loginError: ""
    };
    // 2026-10-08：亲情守护绑定状态
    if (session.guardian !== undefined) {
      next.guardian = session.guardian;
      if (session.guardian) {
        next.guardianForm = {
          name: session.guardian.name,
          phone: session.guardian.phone,
          relation: session.guardian.relation
        };
      }
    }
    // 用户没有手动切过版本时，银发客群默认进入适老版
    const manualMode = wx.getStorageSync("ui_mode_manual");
    if (!manualMode && session.silver !== undefined && session.silver !== null) {
      next.uiMode = session.silver ? "elder" : "normal";
    }
    if (resetChat) {
      next.messages = welcomeMessages();
      next.lastMsgId = "msg-0";
      next.inputContent = "";
    }
    this.setData(next, () => this.startNoticePoll());
    wx.setStorageSync("bank_session", {
      userId: session.userId,
      userName: session.userName,
      phoneMasked: session.phoneMasked,
      token: session.token,
      silver: session.silver,
      guardian: session.guardian !== undefined ? session.guardian : this.data.guardian
    });
    this.syncRealBalance();
  },

  toggleUiMode() {
    const next = this.data.uiMode === "elder" ? "normal" : "elder";
    wx.setStorageSync("ui_mode", next);
    wx.setStorageSync("ui_mode_manual", true);
    this.setData({ uiMode: next });
    wx.showToast({
      title: next === "elder" ? "已切换到适老版" : "已切换到普通版",
      icon: "none"
    });
  },

  // ==== 2026-10-08 新增：适老版语音播报 ====
  // 先用普通请求把语音文件拉下来，再播放。这样回复一到，语音请求一定发出去。
  speakText(text) {
    const content = clipSpeech(text);
    if (!content) return;
    if (this._inflightSpeech === content) return;
    const token = (this._speakToken || 0) + 1;
    this._speakToken = token;
    const url = `${API_BASE}/api/tts?text=${encodeURIComponent(content)}`;
    this._inflightSpeech = content;
    wx.downloadFile({
      url,
      success: (res) => {
        if (token !== this._speakToken) return;
        if (res.statusCode !== 200 || !res.tempFilePath) {
          wx.showToast({ title: "语音文件获取失败", icon: "none" });
          return;
        }
        if (this._audio) {
          try {
            this._audio.destroy();
          } catch (e) {
            // 旧基础库没有 destroy
          }
          this._audio = null;
        }
        const audio = wx.createInnerAudioContext();
        audio.obeyMuteSwitch = false;
        audio.volume = 1;
        audio.src = res.tempFilePath;
        audio.onEnded(() => this.setData({ speaking: false }));
        audio.onError((err) => {
          this.setData({ speaking: false });
          wx.showToast({ title: (err && err.errMsg) || "朗读失败", icon: "none" });
        });
        audio.play();
        this._audio = audio;
        this.setData({ speaking: true });
      },
      fail: () => {
        wx.showToast({ title: "语音请求失败", icon: "none" });
      },
      complete: () => {
        if (this._inflightSpeech === content) this._inflightSpeech = "";
      }
    });
  },

  stopSpeak() {
    const previous = this._audio;
    this._audio = null;
    if (previous) {
      try {
        previous.destroy();
      } catch (e) {
        // 旧基础库没有 destroy
      }
    }
    if (this.data.speaking) {
      this.setData({ speaking: false });
    }
  },

  voiceEnabled() {
    const saved = wx.getStorageSync("voice_on");
    return !!(voiceOnFlag || this._voiceOn || this.data.voiceOn || saved === true || saved === "1" || saved === 1);
  },

  // 开启播报后，助手每次回复都会自动念出来
  maybeSpeak(text) {
    if (this.voiceEnabled()) {
      this.speakText(text);
    }
  },

  toggleVoice() {
    const next = !voiceOnFlag;
    voiceOnFlag = next;
    this._voiceOn = next;
    wx.setStorageSync("voice_on", next ? "1" : "0");
    this.setData({ voiceOn: next });
    if (next) {
      this.speakText("语音播报已开启，我会把重要内容念给您听。");
      wx.showToast({ title: "语音播报已开启", icon: "none" });
    } else {
      this.stopSpeak();
      wx.showToast({ title: "语音播报已关闭", icon: "none" });
    }
  },

  // 点击助手的「朗读」按钮。序号放在 data-mid 里，避免 data-index 被列表事件盖掉。
  speakMessage(e) {
    const dataset = (e.currentTarget && e.currentTarget.dataset) || {};
    const raw = String(dataset.mid || "");
    const index = parseInt(raw.replace(/^m/, ""), 10);
    const text = speechFromMessage(this.data.messages[index]);
    if (!text) {
      wx.showToast({ title: "这条没有可朗读的内容", icon: "none" });
      return;
    }
    this.speakText(text);
  },

  // ==== 2026-10-08 新增：紧急联系人（亲情反诈守护）====
  onGuardianField(e) {
    const field = e.currentTarget.dataset.field;
    this.setData({ [`guardianForm.${field}`]: e.detail.value });
  },

  selectGuardianRelation(e) {
    this.setData({ "guardianForm.relation": e.currentTarget.dataset.value });
  },

  saveGuardian() {
    const { name, phone, relation } = this.data.guardianForm;
    if (!/^1[3-9]\d{9}$/.test((phone || "").trim())) {
      wx.showToast({ title: "请填写 11 位手机号", icon: "none" });
      return;
    }
    if (!(name || "").trim()) {
      wx.showToast({ title: "请填写家人姓名", icon: "none" });
      return;
    }
    wx.showLoading({ title: "正在绑定..." });
    wx.request({
      url: `${API_BASE}/api/guardian/bind`,
      method: "POST",
      data: {
        user_id: this.data.userId,
        token: this.data.authToken,
        name: name.trim(),
        phone: phone.trim(),
        relation: relation || "亲属"
      },
      header: { "content-type": "application/json" },
      success: (res) => {
        wx.hideLoading();
        if (res.statusCode === 401) {
          this.handleAuthFailure();
          return;
        }
        const body = res.data || {};
        if (!body.success) {
          wx.showToast({ title: body.detail || "绑定失败", icon: "none" });
          return;
        }
        const messages = [...this.data.messages];
        for (let i = messages.length - 1; i >= 0; i -= 1) {
          if (messages[i].type === "GUARDIAN_CARD") {
            messages[i].cardData = body.data;
            messages[i].text = body.message;
            break;
          }
        }
        this.setData({
          messages,
          guardian: (body.data && body.data.guardian) || null,
          lastMsgId: `msg-${messages.length - 1}`
        });
        wx.showToast({ title: "绑定成功", icon: "success" });
        this.maybeSpeak(body.message);
      },
      fail: () => {
        wx.hideLoading();
        wx.showToast({ title: "网络连接失败", icon: "none" });
      }
    });
  },

  logout() {
    const token = this.data.authToken;
    this.stopSpeak();
    wx.removeStorageSync("bank_session");
    this.setData({
      loggedIn: false,
      userId: "",
      userName: "",
      phoneMasked: "",
      authToken: "",
      balance: "--",
      guardian: null,
      loginPassword: "",
      messages: welcomeMessages(),
      lastMsgId: "msg-0",
      purchaseSheetVisible: false,
      pinSheet: { ...this.data.pinSheet, visible: false, password: "", error: "" }
    });
    if (token) {
      wx.request({
        url: `${API_BASE}/api/auth/logout`,
        method: "POST",
        data: { token },
        header: { "content-type": "application/json" }
      });
    }
  },

  handleAuthFailure() {
    wx.removeStorageSync("bank_session");
    this.setData({
      loggedIn: false,
      authToken: "",
      loginError: "登录已失效，请重新登录"
    });
  },

  // 从后端数据库拉取最新真实活期余额
  syncRealBalance() {
    if (!this.data.loggedIn) return;
    wx.request({
      url: `${API_BASE}/api/user/balance?user_id=${this.data.userId}&token=${this.data.authToken}`,
      method: "GET",
      success: (res) => {
        if (res.statusCode === 401) {
          this.handleAuthFailure();
          return;
        }
        if (res.data && res.data.balance !== undefined) {
          this.setData({
            balance: res.data.balance
          });
        }
      },
      fail: () => {
        console.error("同步账户余额失败，请检查后端服务是否启动");
      }
    });
  },

  onInput(e) {
    this.setData({ inputContent: e.detail.value });
  },

  sendQuickPrompt(e) {
    const text = e.currentTarget.dataset.text;
    this.executeSend(text);
  },

  triggerQuickBuy(e) {
    const pId = e.currentTarget.dataset.pid;
    const pName = e.currentTarget.dataset.pname;
    const pMin = parseFloat(e.currentTarget.dataset.min);
    this.setData({
      purchaseSheetVisible: true,
      purchaseForm: {
        productId: pId,
        productName: pName,
        minAmount: pMin,
        amountInput: ""
      },
      purchaseAmountError: "",
      purchaseCanSubmit: false
    });
  },

  closePurchaseSheet() {
    this.setData({
      purchaseSheetVisible: false,
      purchaseAmountError: "",
      purchaseCanSubmit: false
    });
  },

  onPurchaseAmountInput(e) {
    const raw = (e.detail.value || "").trim();
    const min = this.data.purchaseForm.minAmount;
    const form = { ...this.data.purchaseForm, amountInput: raw };
    let error = "";
    let canSubmit = false;

    if (raw) {
      if (!/^\d+(\.\d{0,2})?$/.test(raw)) {
        error = "请输入有效金额";
      } else {
        const amount = parseFloat(raw);
        if (amount < min) {
          error = `金额需大于等于¥${min}`;
        } else {
          canSubmit = true;
        }
      }
    }

    this.setData({
      purchaseForm: form,
      purchaseAmountError: error,
      purchaseCanSubmit: canSubmit
    });
  },

  submitPurchase() {
    if (!this.data.purchaseCanSubmit) return;
    const { productName, amountInput } = this.data.purchaseForm;
    const amount = parseFloat(amountInput);
    this.closePurchaseSheet();
    this.executeSend(`我想申购${productName}，金额${amount}元`);
  },

  selectOccupation(e) {
    const msgIndex = e.currentTarget.dataset.index;
    const value = e.currentTarget.dataset.value;
    this.setData({
      [`messages[${msgIndex}].cardData.occupation`]: value
    });
  },

  selectSurveyOption(e) {
    const msgIndex = e.currentTarget.dataset.index;
    const qid = e.currentTarget.dataset.qid;
    const value = e.currentTarget.dataset.value;
    this.setData({
      [`messages[${msgIndex}].cardData.answers.${qid}`]: value
    });
  },

  submitRiskSurvey(e) {
    const msgIndex = e.currentTarget.dataset.index;
    const card = this.data.messages[msgIndex].cardData || {};
    const answers = card.answers || {};
    const questions = card.questions || [];
    const missing = questions.filter((q) => !answers[q.id]).map((q) => q.no);
    if (!card.occupation) {
      wx.showToast({ title: "请选择职业", icon: "none" });
      return;
    }
    if (missing.length) {
      wx.showToast({ title: `请完成第${missing[0]}题`, icon: "none" });
      return;
    }

    wx.showLoading({ title: "评估中..." });
    wx.request({
      url: `${API_BASE}/api/risk/survey`,
      method: "POST",
      data: {
        user_id: this.data.userId,
        token: this.data.authToken,
        answers,
        occupation: card.occupation
      },
      header: { "content-type": "application/json" },
      success: (res) => {
        wx.hideLoading();
        if (res.statusCode === 401) {
          this.handleAuthFailure();
          return;
        }
        const body = res.data || {};
        if (res.statusCode >= 400 || !body.success) {
          const detail = (body.detail) || "提交失败";
          wx.showToast({ title: typeof detail === "string" ? detail : "请完成全部题目", icon: "none" });
          return;
        }
        const updated = [...this.data.messages];
        updated[msgIndex].surveySubmitted = true;
        const reco = body.recommendation || {};
        if (reco.type) {
          updated.push({
            role: "assistant",
            type: reco.type,
            text: reco.text,
            cardData: this.enrichCardData(reco.card_data),
            confirmed: false,
            cancelled: false
          });
        } else {
          updated.push({
            role: "assistant",
            type: "TEXT",
            text: body.message
          });
        }
        this.setData({
          messages: updated,
          lastMsgId: `msg-${updated.length - 1}`
        });
        this.maybeSpeak(reco.text || body.message || "风险测评已完成");
      },
      fail: () => {
        wx.hideLoading();
        wx.showToast({ title: "网络连接失败", icon: "none" });
      }
    });
  },

  triggerProductDetail(e) {
    const pName = e.currentTarget.dataset.pname;
    this.executeSend(`介绍一下${pName}`);
  },

  sendMessage() {
    if (this.data.sending) return;
    const text = this.data.inputContent.trim();
    if (!text) return;
    this.executeSend(text);
  },

  executeSend(text) {
    // 防重复：轻提示、回车、快捷按钮可能在同一时刻触发
    if (this.data.sending) return;
    const speakThisReply = this.voiceEnabled();
    const userMessage = { role: "user", type: "TEXT", text: text };
    const currentMessages = this.data.messages.concat(userMessage);

    this.setData({
      messages: currentMessages,
      inputContent: "",
      sending: true,
      // 滚动锚点指向「正在输入」气泡，保证长消息下也能跟随到底部
      lastMsgId: "typing-indicator"
    });

    wx.request({
      url: `${API_BASE}/api/chat`,
      method: "POST",
      data: {
        user_id: this.data.userId,
        token: this.data.authToken,
        message: text
      },
      header: { "content-type": "application/json" },
      success: (res) => {
        this.setData({ sending: false });
        if (res.statusCode === 401) {
          this.handleAuthFailure();
          return;
        }
        const data = res.data || {};
        // 2026-10-06：后端返回错误时给出可见提示，避免出现「只有头像没有内容」的空白气泡
        if (res.statusCode >= 400 || !data.type) {
          const rawDetail = data.detail || data.message;
          const detail =
            typeof rawDetail === "string" && rawDetail
              ? rawDetail
              : "服务暂时不可用，请稍后重试";
          const errMessages = this.data.messages.concat({
            role: "assistant",
            type: "TEXT",
            text: detail,
            // 带上原文，失败后可以一键重发
            retryText: text
          });
          this.setData({
            messages: errMessages,
            lastMsgId: `msg-${errMessages.length - 1}`
          });
          this.maybeSpeak(detail);
          console.error("chat 接口返回异常", res.statusCode, data);
          return;
        }
        const cardData = this.enrichCardData(data.card_data);
        let spoken = data.speech || data.text || "";
        try {
          spoken = speechFromMessage({
            role: "assistant",
            type: data.type,
            text: data.text,
            cardData: cardData
          }) || spoken;
        } catch (err) {
          spoken = data.speech || data.text || "";
        }
        if (!spoken) spoken = "助手已回复，请看屏幕上的内容。";
        if (speakThisReply) {
          this.speakText(spoken);
        }
        const newAssistantMsg = {
          role: "assistant",
          type: data.type,
          text: data.text,
          cardData: cardData,
          confirmed: false,
          cancelled: false
        };
        const updatedList = this.data.messages.concat(newAssistantMsg);
        const nextData = {
          messages: updatedList,
          lastMsgId: `msg-${updatedList.length - 1}`
        };
        if (typeof data.current_balance === "number") {
          nextData.balance = data.current_balance;
        }
        this.setData(nextData);
        if (!speakThisReply) {
          this.maybeSpeak(spoken);
        }
      },
      fail: () => {
        const errMessages = this.data.messages.concat({
          role: "assistant",
          type: "TEXT",
          text: "网络连接失败，请检查网络后重试。",
          retryText: text
        });
        this.setData({
          sending: false,
          messages: errMessages,
          lastMsgId: `msg-${errMessages.length - 1}`
        });
      }
    });
  },

  // 2026-10-23 新增：失败后一键重发。先摘掉报错气泡和上一条用户消息，避免重复刷屏。
  retryMessage(e) {
    const text = e.currentTarget.dataset.text;
    if (!text || this.data.sending) return;
    const messages = this.data.messages.slice();
    if (messages.length && messages[messages.length - 1].retryText) {
      messages.pop();
    }
    if (messages.length && messages[messages.length - 1].role === "user") {
      messages.pop();
    }
    this.setData({ messages }, () => this.executeSend(text));
  },

  enrichCardData(cardData) {
    if (!cardData) return null;
    const kind = cardData.card_kind || "transfer";
    const titles = {
      transfer: "转账核对确认单",
      wealth: "理财申购确认单",
      card: "卡片操作确认单",
      sub_cancel: "取消订阅确认单",
      cross_lock: "心愿备用金确认单",
      cross_release: "取回心愿备用金"
    };
    const labels = {
      transfer: "核对无误，立即转账",
      wealth: "确认申购",
      card: "确认执行",
      sub_cancel: "确认取消续费",
      cross_lock: "确认存入",
      cross_release: "确认取回"
    };
    return {
      ...cardData,
      card_title: cardData.card_title || titles[kind] || "业务确认单",
      confirm_label: labels[kind] || "确认办理",
      // 2026-10-06：后端可显式指定需密码（如调整转账限额），优先采用后端标记
      requires_password:
        cardData.requires_password !== undefined
          ? !!cardData.requires_password
          : SENSITIVE_KINDS.indexOf(kind) >= 0
    };
  },

  cancelOrder(e) {
    const orderId = e.currentTarget.dataset.orderid;
    const msgIndex = e.currentTarget.dataset.index;

    wx.request({
      url: `${API_BASE}/api/order/cancel`,
      method: "POST",
      data: {
        user_id: this.data.userId,
        token: this.data.authToken,
        order_id: orderId
      },
      header: { "content-type": "application/json" },
      success: (res) => {
        if (res.statusCode === 401) {
          this.handleAuthFailure();
          return;
        }
        const resp = res.data;
        if (resp.success) {
          const updatedMessages = [...this.data.messages];
          updatedMessages[msgIndex].cancelled = true;
          updatedMessages.push({
            role: "assistant",
            type: "TEXT",
            text: resp.message || "已取消本次操作"
          });
          this.setData({
            messages: updatedMessages,
            lastMsgId: `msg-${updatedMessages.length - 1}`
          });
          this.maybeSpeak(resp.message || "已取消本次操作");
        }
      }
    });
  },

  confirmOrder(e) {
    const orderId = e.currentTarget.dataset.orderid;
    const msgIndex = e.currentTarget.dataset.index;
    const card = (this.data.messages[msgIndex] && this.data.messages[msgIndex].cardData) || {};
    if (card.payment_cards && card.payment_cards.length) {
      const selected = (card.payment_cards || []).find(
        (src) => src.card_id === card.selected_card_id && src.payable
      );
      if (!selected) {
        wx.showToast({ title: "请选择付款储蓄卡", icon: "none" });
        return;
      }
    }
    if (card.requires_password) {
      const kind = card.card_kind || "transfer";
      const titles = {
        transfer: "确认转账",
        wealth: "确认申购",
        cross_lock: "存入心愿备用金",
        cross_release: "取回心愿备用金",
        card: "确认调整转账限额"
      };
      let summary = card.confirm_label || "请输入交易密码";
      if (kind === "transfer") {
        summary = card.payee_card
          ? `向 ${card.payee}（${card.payee_card}）转出 ¥${card.amount}`
          : `向 ${card.payee} 转出 ¥${card.amount}`;
      }
      if (kind === "wealth") summary = `申购 ${card.product_name} ¥${card.amount}`;
      if (kind === "cross_lock") summary = `为 ${card.beneficiary} 存入心愿备用金 ¥${card.amount || ""}`;
      if (kind === "cross_release") summary = `取回「${card.notes || "心愿备用金"}」约 ¥${card.amount || ""}`;
      if (kind === "card") {
        summary = `${card.card_type || "银行卡"} ${card.card_id || ""} · ${card.new_limit_text || card.action_label || "调整限额"}`;
      }
      this.setData({
        pinSheet: {
          visible: true,
          orderId,
          msgIndex,
          title: titles[kind] || "请输入交易密码",
          summary,
          password: "",
          error: "",
          fraud: card.fraud && card.fraud.level && card.fraud.level !== "normal" ? card.fraud : null,
          riskAck: false
        }
      });
      return;
    }
    this.submitConfirm(orderId, msgIndex, "", false);
  },

  closePinSheet() {
    this.setData({
      pinSheet: { ...this.data.pinSheet, visible: false, password: "", error: "" }
    });
  },

  onPinInput(e) {
    this.setData({
      "pinSheet.password": e.detail.value,
      "pinSheet.error": ""
    });
  },

  toggleRiskAck() {
    this.setData({ "pinSheet.riskAck": !this.data.pinSheet.riskAck });
  },

  submitPin() {
    const sheet = this.data.pinSheet;
    if (!/^\d{6}$/.test(sheet.password || "")) {
      this.setData({ "pinSheet.error": "请输入 6 位数字交易密码" });
      return;
    }
    if (sheet.fraud && sheet.fraud.require_ack && !sheet.riskAck) {
      this.setData({ "pinSheet.error": "请先勾选已阅读风险提示" });
      return;
    }
    this.submitConfirm(sheet.orderId, sheet.msgIndex, sheet.password, !!sheet.riskAck);
  },

  selectPayCard(e) {
    const msgIndex = e.currentTarget.dataset.index;
    const cardId = e.currentTarget.dataset.cardid;
    const raw = e.currentTarget.dataset.payable;
    const payable = raw === true || raw === "true" || raw === 1 || raw === "1";
    if (!payable) {
      wx.showToast({ title: "请使用储蓄卡付款", icon: "none" });
      return;
    }
    this.setData({
      [`messages[${msgIndex}].cardData.selected_card_id`]: cardId
    });
  },

  submitConfirm(orderId, msgIndex, password, riskAck) {
    const card = (this.data.messages[msgIndex] && this.data.messages[msgIndex].cardData) || {};
    wx.showLoading({ title: "正在核验..." });

    wx.request({
      url: `${API_BASE}/api/transfer/confirm`,
      method: "POST",
      data: {
        user_id: this.data.userId,
        token: this.data.authToken,
        order_id: orderId,
        password,
        risk_ack: riskAck,
        pay_card_id: card.selected_card_id || ""
      },
      header: { "content-type": "application/json" },
      success: (res) => {
        wx.hideLoading();
        if (res.statusCode === 401) {
          this.handleAuthFailure();
          return;
        }
        const resp = res.data || {};
        const message = resp.message || (typeof resp.detail === "string" ? resp.detail : "");

        if (resp.success) {
          const updatedMessages = [...this.data.messages];
          updatedMessages[msgIndex].confirmed = true;
          updatedMessages.push({
            role: "assistant",
            type: "TEXT",
            text: resp.message
          });

          this.setData({
            messages: updatedMessages,
            balance: resp.current_balance,
            lastMsgId: `msg-${updatedMessages.length - 1}`,
            pinSheet: { ...this.data.pinSheet, visible: false, password: "", error: "" }
          });
          this.maybeSpeak(resp.message);
        } else if (resp.password_error) {
          this.setData({
            "pinSheet.password": "",
            "pinSheet.error": resp.message || "交易密码错误"
          });
        } else if (resp.limit_block) {
          // 2026-10-06 新增：确认时额度校验未通过，展示额度说明与一键调额入口
          this.showLimitAlert(resp.limit_block, message);
        } else {
          this.setData({
            "pinSheet.password": "",
            "pinSheet.error": message || "操作失败"
          });
        }
      },
      fail: () => {
        wx.hideLoading();
        wx.showToast({ title: "请求超时", icon: "none" });
      }
    });
  },

  showLimitAlert(block, message) {
    const money = (v) => (v === null || v === undefined ? "不限" : `¥${Number(v).toFixed(2)}`);
    const sections = [];
    if (block.card_id) {
      sections.push({
        label: "付款卡",
        value: `${block.card_type || "储蓄卡"} ${block.card_id}（${block.card_status_label || "—"}）`
      });
    }
    if (block.current_remaining_daily_amount != null) {
      sections.push({ label: "今日剩余可转", value: money(block.current_remaining_daily_amount) });
    }
    if (block.current_remaining_daily_count != null) {
      sections.push({ label: "今日剩余笔数", value: `${block.current_remaining_daily_count} 笔` });
    }
    if (block.current_remaining_annual_amount != null) {
      sections.push({ label: "年度剩余可转", value: money(block.current_remaining_annual_amount) });
    }

    const updatedMessages = [...this.data.messages];
    updatedMessages.push({
      role: "assistant",
      type: "INFO_CARD",
      text: message || block.message || "转账额度校验未通过",
      cardData: this.enrichCardData({
        card_kind: "limit_alert",
        title: "转账额度校验未通过",
        sections,
        highlights: block.lines || [],
        quick_actions: block.quick_actions || []
      })
    });

    this.setData({
      messages: updatedMessages,
      lastMsgId: `msg-${updatedMessages.length - 1}`,
      pinSheet: { ...this.data.pinSheet, visible: false, password: "", error: "" }
    });
    this.maybeSpeak(message || block.message || "转账额度校验未通过");
  }
});
