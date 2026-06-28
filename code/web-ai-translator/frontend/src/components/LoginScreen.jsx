import { useState } from 'react';
import API_URL from '../api.js';

// ── Shared security questions ─────────────────────────────────────────────────
const SECURITY_QUESTIONS = [
  'Tên thú cưng đầu tiên của bạn là gì?',
  'Tên trường tiểu học của bạn là gì?',
  'Tên thành phố bạn sinh ra là gì?',
  'Biệt danh thời thơ ấu của bạn là gì?',
  'Tên người thầy/cô giáo yêu thích của bạn là gì?',
  'Tên đường bạn lớn lên là gì?',
];

// ── Sub-forms ─────────────────────────────────────────────────────────────────

function LoginForm({ onLogin, onGotoRegister, onGotoForgot }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setLoading(true);
    setError('');
    try {
      const res = await fetch(`${API_URL}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail || 'Đăng nhập thất bại'); return; }
      onLogin(data.token);
    } catch {
      setError('Không kết nối được với server');
    } finally {
      setLoading(false);
    }
  }

  return (
    <form className="login-card" onSubmit={handleSubmit}>
      <div className="login-logo">
        <span className="login-logo-icon">⟨/⟩</span>
        <span className="login-logo-text">Web AI Translator</span>
      </div>
      <h2 className="login-title">Đăng nhập</h2>

      <div className="login-field">
        <label htmlFor="login-username">Tên đăng nhập</label>
        <input id="login-username" type="text" autoComplete="username"
          value={username} onChange={e => setUsername(e.target.value)}
          placeholder="Nhập tên đăng nhập" disabled={loading} autoFocus />
      </div>

      <div className="login-field">
        <label htmlFor="login-password">Mật khẩu</label>
        <input id="login-password" type="password" autoComplete="current-password"
          value={password} onChange={e => setPassword(e.target.value)}
          placeholder="Nhập mật khẩu" disabled={loading} />
      </div>

      {error && <div className="login-error">{error}</div>}

      <button className="login-btn" type="submit"
        disabled={loading || !username.trim() || !password}>
        {loading ? 'Đang đăng nhập...' : 'Đăng nhập'}
      </button>

      <div className="login-links">
        <button type="button" className="login-link" onClick={onGotoRegister}>
          Tạo tài khoản mới
        </button>
        <span className="login-link-sep">·</span>
        <button type="button" className="login-link" onClick={onGotoForgot}>
          Quên mật khẩu?
        </button>
      </div>
    </form>
  );
}

function RegisterForm({ onSuccess, onBack }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [question, setQuestion] = useState(SECURITY_QUESTIONS[0]);
  const [answer, setAnswer] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (password !== confirm) { setError('Mật khẩu xác nhận không khớp'); return; }
    setLoading(true);
    setError('');
    try {
      const res = await fetch(`${API_URL}/api/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username.trim(),
          password,
          security_question: question,
          security_answer: answer.trim(),
        }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail || 'Đăng ký thất bại'); return; }
      onSuccess(username.trim());
    } catch {
      setError('Không kết nối được với server');
    } finally {
      setLoading(false);
    }
  }

  return (
    <form className="login-card" onSubmit={handleSubmit}>
      <div className="login-logo">
        <span className="login-logo-icon">⟨/⟩</span>
        <span className="login-logo-text">Web AI Translator</span>
      </div>
      <h2 className="login-title">Tạo tài khoản</h2>

      <div className="login-field">
        <label>Tên đăng nhập</label>
        <input type="text" autoComplete="username"
          value={username} onChange={e => setUsername(e.target.value)}
          placeholder="Nhập tên đăng nhập" disabled={loading} autoFocus />
      </div>

      <div className="login-field">
        <label>Mật khẩu <span className="login-hint">(ít nhất 6 ký tự)</span></label>
        <input type="password" autoComplete="new-password"
          value={password} onChange={e => setPassword(e.target.value)}
          placeholder="Nhập mật khẩu" disabled={loading} />
      </div>

      <div className="login-field">
        <label>Xác nhận mật khẩu</label>
        <input type="password" autoComplete="new-password"
          value={confirm} onChange={e => setConfirm(e.target.value)}
          placeholder="Nhập lại mật khẩu" disabled={loading} />
      </div>

      <div className="login-field">
        <label>Câu hỏi bảo mật</label>
        <select value={question} onChange={e => setQuestion(e.target.value)} disabled={loading}>
          {SECURITY_QUESTIONS.map(q => <option key={q} value={q}>{q}</option>)}
        </select>
      </div>

      <div className="login-field">
        <label>Câu trả lời bảo mật</label>
        <input type="text" autoComplete="off"
          value={answer} onChange={e => setAnswer(e.target.value)}
          placeholder="Nhập câu trả lời" disabled={loading} />
      </div>

      {error && <div className="login-error">{error}</div>}

      <button className="login-btn" type="submit"
        disabled={loading || !username.trim() || !password || !confirm || !answer.trim()}>
        {loading ? 'Đang tạo tài khoản...' : 'Tạo tài khoản'}
      </button>

      <div className="login-links">
        <button type="button" className="login-link" onClick={onBack}>
          ← Quay lại đăng nhập
        </button>
      </div>
    </form>
  );
}

function ForgotPasswordForm({ onSuccess, onBack }) {
  // Step 1: enter username → fetch security question
  // Step 2: answer security question
  // Step 3: set new password. Backend verifies the saved answer here.
  const [step, setStep] = useState('username');
  const [username, setUsername] = useState('');
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function fetchQuestion(e) {
    e.preventDefault();
    if (!username.trim()) return;
    setLoading(true);
    setError('');
    try {
      const res = await fetch(
        `${API_URL}/api/auth/security-question?username=${encodeURIComponent(username.trim())}`
      );
      const data = await res.json();
      if (!res.ok) { setError(data.detail || 'Không tìm thấy tài khoản'); return; }
      setQuestion(data.security_question);
      setStep('question');
    } catch {
      setError('Không kết nối được với server');
    } finally {
      setLoading(false);
    }
  }

  function submitAnswer(e) {
    e.preventDefault();
    if (!answer.trim()) return;
    setError('');
    setStep('reset');
  }

  async function submitReset(e) {
    e.preventDefault();
    if (newPassword !== confirm) { setError('Mật khẩu xác nhận không khớp'); return; }
    setLoading(true);
    setError('');
    try {
      const res = await fetch(`${API_URL}/api/auth/forgot-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username.trim(),
          security_answer: answer.trim(),
          new_password: newPassword,
        }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail || 'Đặt lại mật khẩu thất bại'); return; }
      onSuccess();
    } catch {
      setError('Không kết nối được với server');
    } finally {
      setLoading(false);
    }
  }

  const title = step === 'reset' ? 'Đặt lại mật khẩu' : 'Quên mật khẩu';
  const subtitle = step === 'username'
    ? ''
    : step === 'question'
      ? 'Vui lòng trả lời câu hỏi bảo mật của bạn để đặt lại mật khẩu.'
      : 'Nhập mật khẩu mới cho tài khoản của bạn.';

  return (
    <form
      className={`login-card forgot-card forgot-step-${step}`}
      onSubmit={step === 'username' ? fetchQuestion : step === 'question' ? submitAnswer : submitReset}
    >
      <div className="login-logo">
        <span className="login-logo-icon">⟨/⟩</span>
        <span className="login-logo-text">Web AI Translator</span>
      </div>
      <h2 className="login-title">{title}</h2>
      {subtitle && <p className="login-subtitle">{subtitle}</p>}

      {step === 'username' && (
        <div className="login-field">
          <label>Tên đăng nhập</label>
          <input type="text" value={username}
            onChange={e => setUsername(e.target.value)}
            placeholder="Nhập tên đăng nhập" disabled={loading} autoFocus />
        </div>
      )}

      {step === 'question' && (
        <>
          <div className="login-field">
            <label>Tên đăng nhập</label>
            <input type="text" value={username} disabled />
          </div>

          <div className="login-field">
            <label>Câu hỏi bảo mật</label>
            <p className="login-question">{question}</p>
          </div>

          <div className="login-field">
            <label>Câu trả lời</label>
            <input type="text" autoComplete="off"
              value={answer} onChange={e => setAnswer(e.target.value)}
              placeholder="Nhập câu trả lời" disabled={loading} />
          </div>
        </>
      )}

      {step === 'reset' && (
        <>
          <div className="login-field">
            <label>Mật khẩu mới <span className="login-hint">(ít nhất 6 ký tự)</span></label>
            <input type="password" autoComplete="new-password"
              value={newPassword} onChange={e => setNewPassword(e.target.value)}
              placeholder="Nhập mật khẩu mới" disabled={loading} />
          </div>

          <div className="login-field">
            <label>Xác nhận mật khẩu mới</label>
            <input type="password" autoComplete="new-password"
              value={confirm} onChange={e => setConfirm(e.target.value)}
              placeholder="Nhập lại mật khẩu mới" disabled={loading} />
          </div>

          <div className="password-requirements">
            <p>Yêu cầu mật khẩu:</p>
            <span>✓ Tối thiểu 6 ký tự</span>
            <span>✓ Nên gồm chữ hoa, chữ thường và số</span>
          </div>
        </>
      )}

      {error && <div className="login-error">{error}</div>}

      <button className="login-btn" type="submit"
        disabled={
          loading
          || (step === 'username' && !username.trim())
          || (step === 'question' && !answer.trim())
          || (step === 'reset' && (!newPassword || !confirm))
        }>
        {loading
          ? (step === 'username'
            ? 'Đang tìm tài khoản...'
            : step === 'question'
              ? 'Đang xác nhận...'
              : 'Đang đặt lại...')
          : (step === 'username' ? 'Tiếp tục' : step === 'question' ? 'Xác nhận câu trả lời' : 'Cập nhật mật khẩu')}
      </button>

      <div className="login-links">
        <button
          type="button"
          className="login-link"
          onClick={() => {
            setError('');
            if (step === 'username') onBack();
            else if (step === 'question') setStep('username');
            else setStep('question');
          }}
        >
          {step === 'username' ? '← Quay lại đăng nhập' : step === 'question' ? '← Nhập lại tên đăng nhập' : '← Quay lại câu hỏi bảo mật'}
        </button>
      </div>
    </form>
  );
}

// ── Main screen orchestrator ──────────────────────────────────────────────────

export default function LoginScreen({ onLogin }) {
  // view: 'login' | 'register' | 'forgot'
  const [view, setView] = useState('login');
  const [successMsg, setSuccessMsg] = useState('');

  function handleRegisterSuccess(username) {
    setSuccessMsg(`Tài khoản "${username}" đã được tạo. Vui lòng đăng nhập.`);
    setView('login');
  }

  function handleResetSuccess() {
    setSuccessMsg('Mật khẩu đã được đặt lại. Vui lòng đăng nhập với mật khẩu mới.');
    setView('login');
  }

  return (
    <div className="login-overlay">
      {successMsg && view === 'login' && (
        <div className="login-success-banner">{successMsg}</div>
      )}
      {view === 'login' && (
        <LoginForm
          onLogin={onLogin}
          onGotoRegister={() => { setSuccessMsg(''); setView('register'); }}
          onGotoForgot={() => { setSuccessMsg(''); setView('forgot'); }}
        />
      )}
      {view === 'register' && (
        <RegisterForm
          onSuccess={handleRegisterSuccess}
          onBack={() => setView('login')}
        />
      )}
      {view === 'forgot' && (
        <ForgotPasswordForm
          onSuccess={handleResetSuccess}
          onBack={() => setView('login')}
        />
      )}
    </div>
  );
}
