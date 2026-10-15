const interpret = require('./ai-interpret.js');
const PROVINCES = require('./provinces.js');
const { todayISO } = require('./format.js');

const QUALIFICATIONS = ['高新技术企业', '科技型中小企业', '专精特新中小企业', '专精特新“小巨人”企业', '创新型中小企业'];
const SECTIONS = [['对象', '申报对象'], ['条件', '申报条件']];
const EMPTY_COMPANY = '请自行对照原文';
const text = (value) => value == null ? '' : String(value).trim();

function emptyForm() {
  return { established: '', region: '', city: '', revenueAmount: '', revenueYear: '', rdAmount: '', rdYear: '', employees: '', qualification: [] };
}
function validDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split('-').map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1];
}

function validateForm(form = {}, today = todayISO()) {
  const errors = {}; const year = Number(today.slice(0, 4));
  const established = text(form.established);
  if (established && (!validDate(established) || established > today)) errors.established = '成立日期须为有效日期，不能晚于今天';
  if (text(form.region) && !PROVINCES.includes(form.region)) errors.region = '请从省份清单选择注册地';
  if (text(form.city) && (!text(form.region) || text(form.city).length > 32 || /[/,，;；\n\r]/.test(text(form.city)))) errors.city = '填写城市时请先选择省份，城市须为单个城市名称';
  for (const field of ['revenue', 'rd']) {
    const amount = text(form[`${field}Amount`]); const stamp = text(form[`${field}Year`]);
    if (!amount && !stamp) continue;
    if (!amount || !/^\d+(\.\d{1,2})?$/.test(amount) || !Number.isFinite(Number(amount)) || Number(amount) > 1000000000) {
      errors[`${field}Amount`] = '金额格式不对：请填写数字，单位万元，至多两位小数';
    }
    if (!/^\d{4}$/.test(stamp) || Number(stamp) < 2000 || Number(stamp) > year) errors[`${field}Year`] = `请填写 2000 到 ${year} 的四位年份`;
  }
  const employees = text(form.employees);
  if (employees && (!/^\d+$/.test(employees) || Number(employees) > 1000000)) errors.employees = '职工人数须为 0 到 1000000 的整数';
  const qualification = form.qualification || [];
  if (!Array.isArray(qualification) || qualification.length > 6) errors.qualification = '已有资质至多填写 6 项';
  else qualification.forEach((item, index) => {
    if (!item || !QUALIFICATIONS.includes(item.name)) errors.qualification = '请从资质清单选择';
    const until = text(item && item.valid_until);
    if (until && !validDate(until)) errors[`qualification${index}`] = '请填写有效日期';
  });
  return errors;
}

function toCompany(form = {}, today = todayISO()) {
  const errors = validateForm(form, today);
  if (Object.keys(errors).length) throw Object.assign(new Error('请检查企业情况'), { errors });
  const company = {};
  for (const field of ['established', 'region', 'city']) if (text(form[field])) company[field] = text(form[field]);
  for (const field of ['revenue', 'rd']) if (text(form[`${field}Amount`])) {
    company[field] = { amount: Number(form[`${field}Amount`]), year: Number(form[`${field}Year`]) };
  }
  if (text(form.employees)) company.employees = Number(form.employees);
  if ((form.qualification || []).length) company.qualification = form.qualification.map((item) => ({ name: item.name, valid_until: text(item.valid_until) || null }));
  return company;
}

function resultView(result = {}) {
  const base = interpret.resultView(result);
  return Object.assign({}, base, {
    sections: result.refusal ? [] : SECTIONS.map(([type, title]) => {
      const items = (result.rows || []).filter((row) => row.type === type).map((row, index) => ({
        key: index, text: interpret.oneLine(row.text), refs: (row.refs || []).map((ref, key) => ({ key, pid: ref.pid, quote: interpret.oneLine(ref.quote) })),
        company: (row.company || []).map((item, key) => ({ key: item.key || key, label: item.label, value: item.value })),
        companyHint: (row.company || []).length ? '' : EMPTY_COMPANY,
      }));
      return { type, title, items, missing: !items.length };
    }),
  });
}

function createSession(options) {
  return interpret.createSession(Object.assign({}, options, { kind: 'clause', viewResult: resultView, useTaskMetadata: true }));
}

module.exports = { PROVINCES, QUALIFICATIONS, SECTIONS, EMPTY_COMPANY, emptyForm, validDate, validateForm, toCompany, resultView, createSession };
