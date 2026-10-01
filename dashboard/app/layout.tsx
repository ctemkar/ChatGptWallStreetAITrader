import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  title: 'Northstar LS · Portfolio Command Center',
  description: 'A paper-first U.S. equity long/short strategy dashboard powered by Alpaca.',
  openGraph: {
    title: 'Northstar LS',
    description: 'Portfolio Command Center',
    images: ['/og.png'],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Northstar LS',
    description: 'Portfolio Command Center',
    images: ['/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
