import type { Theme } from './api'

// The toolbar button steps through every theme, so a theme's toolbar label and
// title name the one it switches to.
export const THEME_CYCLE: Record<Theme, Theme> = {
  light: 'gray',
  gray: 'dark',
  dark: 'light',
}

export const THEME_LABELS: Record<Theme, string> = {
  light: '浅色',
  gray: '灰色',
  dark: '深色',
}
